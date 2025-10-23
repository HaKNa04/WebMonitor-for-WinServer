import argparse
import ctypes
import datetime
import json
import logging
import os
import platform
import psutil
import signal
import subprocess
import sys
import time

from app_config import *
from collections import deque
from database import MariaDB
from LHML import Metrics, wmi_module
from nv_api import NVMLHelper
from typing import Dict, List, Optional, Tuple

def _is_admin() -> bool:
    if os.name != "nt":
        return True
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False

def dump_psutil_threads_raw(out_path: str = "Psutil.threads.dump.txt") -> Tuple[int, int]:
    """
    导出 psutil 的原始“进程 + 线程”快照到文本文件（未做业务处理，仅尽量完整输出）。
    返回值: (进程数, 线程行数)
    """
    proc_cnt = 0
    thread_lines = 0
    lines: List[str] = []
    ts = datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    lines.append(f"# psutil processes/threads snapshot at {ts}")
    lines.append("# 注意：仅使用 psutil 原始 API 枚举，可能因权限/竞态导致个别进程访问失败。")
    try:
        # 为稳定性按 PID 升序
        procs = sorted(psutil.process_iter(attrs=["pid", "name", "exe", "cmdline", "username", "create_time", "num_threads"]), key=lambda p: p.info.get("pid") or 0)
        for p in procs:
            try:
                info = p.info
                pid = info.get("pid")
                name = info.get("name") or ""
                exe = info.get("exe") or ""
                cmd = info.get("cmdline") or []
                username = info.get("username") or ""
                ctime = info.get("create_time")
                nthreads = info.get("num_threads")
                try:
                    ctime_str = datetime.datetime.fromtimestamp(ctime).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z") if ctime else "N/A"
                except Exception:
                    ctime_str = "N/A"

                lines.append(f"\nPID={pid} NAME='{name}' EXE='{exe}'")
                lines.append(f"  CMDLINE={cmd!r}")
                lines.append(f"  USER='{username}' CREATE_TIME='{ctime_str}' NUM_THREADS={nthreads}")
                # 线程列表
                try:
                    ths = p.threads()  # List[pcputimes(user_time, system_time), id]
                except Exception as e_th:
                    lines.append(f"  <threads> AccessDenied/NoSuchProcess: {e_th}")
                    ths = []
                for t in ths:
                    # t.id, t.user_time, t.system_time
                    lines.append(f"  - TID={getattr(t, 'id', 'N/A')} user_time={getattr(t, 'user_time', 'N/A')} system_time={getattr(t, 'system_time', 'N/A')}")
                    thread_lines += 1
                proc_cnt += 1
            except Exception as e_p:
                lines.append(f"\nPID=? <process_iter item error>: {e_p}")
                continue
    except Exception as e:
        lines.append(f"\n<enumeration error>: {e}")
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception as e:
        logging.error(f"写入 {out_path} 失败: {e}")
    return proc_cnt, thread_lines

# Windows 注册表（用于读取 IIS / Java / 等版本或路径）
try:
    import winreg  # type: ignore
except Exception:
    winreg = None  # 非 Windows 环境容错

def _read_text_or_none(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        logging.error(f"读取模板失败: {path} -> {e}")
        return None

def setup_logger(debug: bool) -> None:
    level = logging.DEBUG if debug else logging.INFO
    logging.captureWarnings(True)
    logging.basicConfig(
        level=level,
        format="%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],  # 明确输出到 stdout
        force=True,  # 强制替换已存在的 handlers，确保生效
    )

def install_signal_handlers():
    def _handler(signum, frame):
        logging.info(f"({signum})，正在退出...")
        sys.exit(0)

    for sig in (getattr(signal, "SIGINT", None),
                getattr(signal, "SIGTERM", None),
                getattr(signal, "SIGBREAK", None)):  # Windows 控制台关闭事件
        if sig is not None:
            try:
                signal.signal(sig, _handler)
            except Exception:
                pass


def now_utc() -> datetime.datetime:
    # 返回“UTC 时间的 naive datetime”，以便写入 MariaDB DATETIME（不带时区）
    return datetime.datetime.now(datetime.UTC).replace(tzinfo=None)

# ========= pythonnet + LibreHardwareMonitor 读取器（仅相对路径输出/加载） =========
# 仅加载 .NET Framework CLR（netfx）
try:
    from pythonnet import load as _pyclr_load  # type: ignore
    _pyclr_load("netfx")
except Exception:
    pass
try:
    import clr  # type: ignore
except Exception:
    clr = None  # 未安装 pythonnet 或 CLR 加载失败时为 None

def bytes2human(n: int) -> str:
    symbols = ("B", "KB", "MB", "GB", "TB")
    prefix = {}
    for i, s in enumerate(symbols):
        prefix[s] = 1 << (10 * i)
    for s in reversed(symbols):
        if n >= prefix[s]:
            value = float(n) / prefix[s]
            return f"{value:,.2f} {s}"
    return f"{n} B"

def ensure_files(
    out_dir: str,
    html_name: str,
    js_name: str,
    overwrite: bool = False,
    debug: bool = False,
    html_template_path: Optional[str] = None,
    js_template_path: Optional[str] = None,
    chart_js_path: Optional[str] = None,
) -> None:
    os.makedirs(out_dir, exist_ok=True)
    html_path = os.path.join(out_dir, html_name)
    js_path = os.path.join(out_dir, js_name)

    # 读取模板内容
    html_tpl = _read_text_or_none(html_template_path)
    js_tpl = _read_text_or_none(js_template_path)

    # 写入 HTML
    if overwrite or not os.path.exists(html_path):
        if html_tpl is None:
            logging.warning(f"未提供有效 HTML 模板，跳过写入: {html_template_path}")
        else:
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(html_tpl)
            if debug:
                logging.debug(f"Wrote {html_path}")

    # 写入 Dashboard JS
    if overwrite or not os.path.exists(js_path):
        if js_tpl is None:
            logging.warning(f"未提供有效 JS 模板，跳过写入: {js_template_path}")
        else:
            with open(js_path, "w", encoding="utf-8") as f:
                f.write(js_tpl)
            if debug:
                logging.debug(f"Wrote {js_path}")

    # 写入 Chart.js（本地化）
    if chart_js_path:
        try:
            if os.path.exists(chart_js_path):
                # 目标文件名沿用源文件名
                chart_out = os.path.join(out_dir, os.path.basename(chart_js_path))
                if overwrite or not os.path.exists(chart_out):
                    with open(chart_js_path, "r", encoding="utf-8") as src, open(chart_out, "w", encoding="utf-8") as dst:
                        dst.write(src.read())
                    if debug:
                        logging.debug(f"Wrote {chart_out}")
            else:
                logging.warning(f"未找到本地 Chart.js 文件: {chart_js_path}（将继续使用 HTML 中的配置）")
        except Exception as e:
            logging.error(f"写入本地 Chart.js 失败: {e}")

def write_data_js(
    out_dir: str,
    data_js_name: str,
    data: Dict,
    debug: bool = False,
) -> None:
    path = os.path.join(out_dir, data_js_name)
    with open(path, "w", encoding="utf-8") as f:
        f.write("window.__DASHBOARD_DATA__ = ")
        json.dump(data, f, ensure_ascii=False)
        f.write(";")
    if debug:
        logging.debug(f"Wrote {path} size={os.path.getsize(path)} bytes")

def _try_gpu_adapter_memory_wmi_sum() -> Tuple[Optional[int], Optional[int]]:
    """
    返回(used_bytes_sum, total_bytes_sum)。
    基于 Win32_PerfFormattedData_GPUPerformanceCounters_GPUAdapterMemory 的 DedicatedUsage（MB）。
    总量无法通过该计数器直接获取，返回 None。
    """
    if wmi_module is None or platform.system().lower() != "windows":
        return None, None
    try:
        c = wmi_module.WMI(namespace="root\\CIMV2")
        try:
            items = c.Win32_PerfFormattedData_GPUPerformanceCounters_GPUAdapterMemory()
        except Exception:
            items = []
        used_mb = 0.0
        for it in items:
            try:
                du = getattr(it, "DedicatedUsage", None)  # MB
                if du is not None:
                    used_mb += float(du)
            except Exception:
                continue
        if used_mb > 0:
            return int(used_mb * 1_000_000), None
    except Exception as e:
        logging.debug(f"GPU Adapter Memory(WMI) 读取失败: {e}")
    return None, None

def main():
    parser = argparse.ArgumentParser(description="服务器硬件/进程监控（NVML/WMI + MariaDB + 静态仪表盘）")
    parser.add_argument("--db-host", default=DEFAULT_DB_HOST)
    parser.add_argument("--db-port", type=int, default=DEFAULT_DB_PORT)
    parser.add_argument("--db-user", default=DEFAULT_DB_USER)
    parser.add_argument("--db-pass", default=DEFAULT_DB_PASS)
    parser.add_argument("--db-name", default=DEFAULT_DB_NAME)

    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    parser.add_argument("--html-name", default=DEFAULT_HTML_NAME)
    parser.add_argument("--js-name", default=DEFAULT_JS_NAME)
    parser.add_argument("--data-js-name", default=DEFAULT_DATA_JS_NAME)

    # Web 服务器路径，用于辅助识别（仅用于文件版本读取，不执行命令）
    parser.add_argument("--web-server-path", default=DEFAULT_WEB_SERVER_PATH, help="Web 服务器路径（可执行文件或目录），留空则自动从系统检测（不执行命令）")

    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL, help="采样周期（秒）")
    parser.add_argument("--process-names", default=DEFAULT_PROCESS_NAMES,
                        help="监控进程项，格式：显示名=路径子串；分号或逗号分隔；不含 '=' 时显示名与路径子串相同")
    parser.add_argument("--debug", action="store_true", default=DEFAULT_DEBUG, help="启用调试日志")

    # 速率单位配置
    parser.add_argument("--rate-auto-scale", dest="rate_auto", action="store_true", default=DEFAULT_RATE_AUTO_SCALE, help="速率单位自动切换")
    parser.add_argument("--no-rate-auto-scale", dest="rate_auto", action="store_false", help="禁用速率单位自动切换")
    parser.add_argument("--rate-manual-unit", choices=["1", "K", "M", "G", "T"], default=DEFAULT_RATE_MANUAL_UNIT, help="手动单位等级（当禁用自动时生效）")
    parser.add_argument("--rate-unit-type", choices=["b", "B"], default=DEFAULT_RATE_UNIT_TYPE, help="单位类型：b=bit/s，B=Byte/s")
    parser.add_argument("--html-template", default=DEFAULT_HTML_TEMPLATE_PATH, help="HTML 模板文件路径（index.template.html）")
    parser.add_argument("--js-template", default=DEFAULT_JS_TEMPLATE_PATH, help="Dashboard JS 模板文件路径（dashboard.template.js）")
    parser.add_argument("--chart-js", default=DEFAULT_CHART_JS_PATH, help="本地 Chart.js 文件路径（如 chart.4.4.4.min.js）")
    # 强制覆盖 HTML/JS
    parser.add_argument("--overwrite-assets", action="store_true", default=DEFAULT_OVERWRITE_ASSETS, help="强制覆盖生成的 HTML/JS")

    # 启动清空历史 & 数据保留分钟数（<=0 禁用自动清理）
    parser.add_argument("--wipe-on-start", action="store_true", default=DEFAULT_WIPE_ON_START, help="启动时清空历史数据")
    parser.add_argument("--retention-minutes", type=int, default=DEFAULT_RETENTION_MINUTES, help="数据保留时长（分钟，<=0 表示不清理）")

    # 禁用数据库，仅显示不入库
    parser.add_argument("--no-db", action="store_true", default=DEFAULT_DISABLE_DB, help="禁用数据库，仅生成前端数据，不入库")

    args = parser.parse_args()
    setup_logger(args.debug)
    install_signal_handlers()

    # 启动时导出一次 psutil 原始“进程+线程”快照，便于排查（仅在 DEBUG 模式且文件不存在时执行）
    if args.debug:
        try:
            dump_path = os.path.join(os.getcwd(), "Psutil.threads.dump.txt")
            if not os.path.exists(dump_path):
                pcount, tlines = dump_psutil_threads_raw(dump_path)
                logging.debug(f"已导出 psutil 线程快照: {dump_path} (进程数={pcount}, 线程行数={tlines})")
            else:
                logging.debug(f"检测到已存在快照文件，跳过导出: {dump_path}")
        except Exception as e:
            logging.error(f"导出 psutil 线程快照失败: {e}")

    install_signal_handlers()

    # 启动时检查并输出逻辑 CPU 线程数
    try:
        ncpu_start = max(1, int(psutil.cpu_count(logical=True) or os.cpu_count() or 1))
        logging.debug(f"逻辑 CPU 线程数: {ncpu_start}")
    except Exception as e:
        logging.error(f"读取逻辑 CPU 线程数失败: {e}")

    proc_names_raw = args.process_names.replace(";", ",")
    proc_names = [s.strip() for s in proc_names_raw.split(",") if s.strip()]

    # 数据库：可禁用/失败自动降级
    db: Optional[MariaDB] = None
    if args.no_db:
        logging.info("数据库未启用（仅显示模式）")
    else:
        try:
            db = MariaDB(args.db_host, args.db_port, args.db_user, args.db_pass, args.db_name, debug=args.debug)
            logging.info("数据库已连接")
            if args.wipe_on_start:
                logging.info("启动清空历史数据（TRUNCATE 所有业务表）")
                db.wipe_all()
        except Exception as e:
            logging.error(f"数据库连接失败，进入仅显示模式（不存储）：{e}")
            db = None

    ensure_files(
        out_dir=args.out_dir,
        html_name=args.html_name,
        js_name=args.js_name,
        overwrite=args.overwrite_assets,
        debug=args.debug,
        html_template_path=args.html_template,
        js_template_path=args.js_template,
        chart_js_path=args.chart_js,
    )

    metrics = Metrics(proc_names, debug=args.debug)

    # 启动时仅探测一次硬件/软件版本，并可选入库
    startup_ts = now_utc().replace(microsecond=0)
    sysinfo_once = metrics.system_info()
    swvers_once = metrics.sw_versions(db, args.web_server_path)
    logging.debug(f"启动探测硬件/软件版本完成: sys_info_keys={list(sysinfo_once.keys())}, sw_versions_keys={list(swvers_once.keys())}")

    if db is not None and not args.no_db:
        try:
            db.insert_one(
                "REPLACE INTO sys_info (ts, os_version, cpu_model, ram_total, vram_total, disk_total) VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    startup_ts,
                    sysinfo_once["os_version"],
                    sysinfo_once["cpu_model"],
                    sysinfo_once["ram_total"],
                    sysinfo_once["vram_total"],
                    sysinfo_once["disk_total"],
                ),
            )
            logging.debug("探测信息入库: sys_info")
            db.insert_one(
                "REPLACE INTO sw_versions (ts, nginx, java, python_cuda) VALUES (%s, %s, %s, %s)",
                (startup_ts, swvers_once["nginx"], swvers_once["java"], swvers_once["python_cuda"]),
            )
            logging.debug("探测信息入库: sw_versions")
        except Exception as e:
            logging.error(f"探测信息入库失败: {e}")

    # 仅显示模式：内存环形缓冲，保留近10分钟曲线
    cpu_hist_len = max(12, int(600 / max(1, args.interval)))
    inmem_cpu_series = deque(maxlen=cpu_hist_len)

    # 循环
    while True:
        loop_start = time.time()
        try:
            ts = now_utc().replace(microsecond=0)

            # 采集（仅动态数据）
            try:
                cpu_total, cpu_per_core = metrics.collect_cpu()
                logging.debug(f"采集完成: CPU 总体={cpu_total:.2f}% 核心数={len(cpu_per_core)}")
            except Exception as e:
                logging.error(f"采集 CPU 数据失败: {e}")
                cpu_total, cpu_per_core = 0.0, []

            try:
                st = metrics.cpu_stats()
                logging.debug(f"采集完成: CPU 传感器 freq={st.get('freq_mhz')}MHz fan={st.get('fan_rpm')}RPM temp={st.get('package_temp_c')}C")
            except Exception as e:
                logging.error(f"采集 CPU 传感器失败: {e}")
                st = {"freq_mhz": None, "fan_rpm": None, "package_temp_c": None}

            try:
                net = metrics.net_io_rates()
                logging.debug(f"采集完成: 网络 up={net.get('up_bps')}bps down={net.get('down_bps')}bps")
            except Exception as e:
                logging.error(f"采集 网络数据失败: {e}")
                net = {"up_bps": 0, "down_bps": 0}

            try:
                disk = metrics.disk_io_rates()
                logging.debug(f"采集完成: 磁盘 read={disk.get('read_bps')}bps write={disk.get('write_bps')}bps")
            except Exception as e:
                logging.error(f"采集 磁盘数据失败: {e}")
                disk = {"read_bps": 0, "write_bps": 0}

            try:
                procs = metrics.process_status()
                logging.debug(f"采集完成: 进程检测 项数={len(procs)}")
            except Exception as e:
                logging.error(f"采集 进程检测失败: {e}")
                procs = []

            # 计算 CPU 占用 TOP 10
            top_procs: List[Dict] = []
            try:
                ncpu = getattr(metrics, "ncpu", None) or max(1, int(psutil.cpu_count(logical=True) or os.cpu_count() or 1))
                items: List[Tuple[float, Dict]] = []
                for p in psutil.process_iter(attrs=["pid", "name", "exe"]):
                    try:
                        pid_val = int(p.info.get("pid") or 0)
                        if pid_val == 0:
                            continue  # 屏蔽 System Idle Process
                        cpu_raw = float(p.cpu_percent(interval=None))
                        mem_rss = int(p.memory_info().rss)
                        cpu_norm = cpu_raw / float(ncpu)
                        items.append((cpu_norm, {
                            "pid": pid_val,
                            "name": p.info.get("name") or "",
                            "exe": p.info.get("exe") or "",
                            "cpu_percent": float(cpu_norm),
                            "mem_rss": mem_rss,
                        }))
                    except Exception:
                        continue
                items.sort(key=lambda t: t[0], reverse=True)
                top_procs = [it[1] for it in items[:10]]
                logging.debug(f"采集完成: TOP_PROCS 数量={len(top_procs)}")
            except Exception as e:
                logging.error(f"采集 TOP_PROCS 失败: {e}")
                top_procs = []

            # 无数据库：仅显示，保存到内存曲线
            inmem_cpu_series.append((ts, cpu_total))

            # 有数据库才入库（不写 sys_info/sw_versions）
            if db is not None and not args.no_db:
                try:
                    db.insert_one("REPLACE INTO cpu_total (ts, percent) VALUES (%s, %s)", (ts, cpu_total))
                    logging.debug("入库成功: cpu_total")
                except Exception as e:
                    logging.error(f"入库失败: cpu_total - {e}")

                try:
                    db.insert_many(
                        "REPLACE INTO cpu_core (ts, core_index, percent) VALUES (%s, %s, %s)",
                        [(ts, i, v) for i, v in enumerate(cpu_per_core)],
                    )
                    logging.debug("入库成功: cpu_core")
                except Exception as e:
                    logging.error(f"入库失败: cpu_core - {e}")

                try:
                    db.insert_one(
                        "REPLACE INTO cpu_stats (ts, freq_mhz, fan_rpm, package_temp_c) VALUES (%s, %s, %s, %s)",
                        (ts, st.get("freq_mhz"), st.get("fan_rpm"), st.get("package_temp_c")),
                    )
                    logging.debug("入库成功: cpu_stats")
                except Exception as e:
                    logging.error(f"入库失败: cpu_stats - {e}")

                try:
                    db.insert_one("REPLACE INTO net_io (ts, up_bps, down_bps) VALUES (%s, %s, %s)", (ts, net["up_bps"], net["down_bps"]))
                    logging.debug("入库成功: net_io")
                except Exception as e:
                    logging.error(f"入库失败: net_io - {e}")

                try:
                    db.insert_one("REPLACE INTO disk_io (ts, read_bps, write_bps) VALUES (%s, %s, %s)", (ts, disk["read_bps"], disk["write_bps"]))
                    logging.debug("入库成功: disk_io")
                except Exception as e:
                    logging.error(f"入库失败: disk_io - {e}")

                if procs:
                    try:
                        db.insert_many(
                            "REPLACE INTO process_status (ts, proc_name, instances, cpu_percent, mem_rss) VALUES (%s, %s, %s, %s, %s)",
                            [(ts, p["name"], p["instances"], p["cpu_percent"], p["mem"] if "mem" in p else p["mem_rss"]) for p in procs],
                        )
                        logging.debug("入库成功: process_status")
                    except Exception as e:
                        logging.error(f"入库失败: process_status - {e}")

                if args.retention_minutes and args.retention_minutes > 0:
                    try:
                        db.purge_older_than(args.retention_minutes)
                        logging.debug(f"数据保留清理完成: retention_minutes={args.retention_minutes}")
                    except Exception as e:
                        logging.error(f"执行数据保留清理失败: {e}")

            # 构建 CPU 曲线：优先 DB，若无则用内存
            series: List[Dict] = []
            if db is not None:
                cpu10 = db.query(
                    "SELECT ts, percent FROM cpu_total WHERE ts >= (UTC_TIMESTAMP() - INTERVAL 10 MINUTE) ORDER BY ts ASC"
                )
                for row in cpu10:
                    t_utc_naive = row["ts"]
                    t_local = t_utc_naive.replace(tzinfo=datetime.UTC).astimezone()
                    label = t_local.strftime("%H:%M:%S")
                    series.append({"t_label": label, "v": float(row["percent"])})
            else:
                for t_utc_naive, v in inmem_cpu_series:
                    t_local = t_utc_naive.replace(tzinfo=datetime.UTC).astimezone()
                    label = t_local.strftime("%H:%M:%S")
                    series.append({"t_label": label, "v": float(v)})

            # 生成页面数据
            data = {
                "cpu_total_series": series,
                "cores": [{"index": i, "percent": float(v)} for i, v in enumerate(cpu_per_core)],
                "stats": {
                    "freq_mhz": st.get("freq_mhz"),
                    "fan_rpm": st.get("fan_rpm"),
                    "package_temp_c": st.get("package_temp_c"),
                },
                "net": net,
                "disk": disk,
                "sys_info": {
                    "os_version": sysinfo_once["os_version"],
                    "cpu_model": sysinfo_once["cpu_model"],
                    "ram_total": sysinfo_once["ram_total"],
                    "vram_total": sysinfo_once["vram_total"],
                    "disk_total": sysinfo_once["disk_total"],
                    "ram_total_h": bytes2human(sysinfo_once["ram_total"]) if sysinfo_once["ram_total"] is not None else "N/A",
                    "vram_total_h": bytes2human(sysinfo_once["vram_total"]) if sysinfo_once["vram_total"] else "N/A",
                    "disk_total_h": bytes2human(sysinfo_once["disk_total"]) if sysinfo_once["disk_total"] is not None else "N/A",
                    "mem_usage_line": None,
                    "vram_usage_line": None,
                    "disk_usage_line": None,
                },
                "sw_versions": swvers_once,
                "processes": procs,
                "rate_prefs": {
                    "auto": bool(args.rate_auto),
                    "unit": args.rate_manual_unit,
                    "type": args.rate_unit_type
                },
                "gauge_prefs": {
                  "freq": {"min": DEFAULT_GAUGE_FREQ_MIN, "max": DEFAULT_GAUGE_FREQ_MAX},
                  "fan":  {"min": DEFAULT_GAUGE_FAN_MIN,  "max": DEFAULT_GAUGE_FAN_MAX},
                  "temp": {"min": DEFAULT_GAUGE_TEMP_MIN, "max": DEFAULT_GAUGE_TEMP_MAX},
                  "thresholds": list(DEFAULT_GAUGE_THRESHOLDS),
                  "angles": {"start_deg": DEFAULT_GAUGE_ANGLE_START_DEG, "end_deg": DEFAULT_GAUGE_ANGLE_END_DEG}
                },
                "poll_ms": int(max(1, args.interval) * 1000),
                "top_procs": top_procs,
                "custom_area": {"img":"https://haviss.cn/MCSMResources/10.webp"},  # 可填 {"html":"..."} 或 {"kv":[["键","值"], ...]}
                "generated_at": f"{ts.strftime('%Y-%m-%d %H:%M:%S')} UTC",
            }

            # ——— 三项动态使用情况 ———
            # 1) 内存
            try:
                vm = psutil.virtual_memory()
                mem_used_b = int(vm.used)
                mem_pct = int(round(vm.percent))
                data["sys_info"]["mem_usage_line"] = f"{bytes2human(mem_used_b)} ({mem_pct}%)"
            except Exception:
                data["sys_info"]["mem_usage_line"] = "N/A"

            # 2) 存储
            try:
                disk_total_dyn = 0
                disk_used_dyn = 0
                for p in psutil.disk_partitions(all=False):
                    if p.fstype and p.mountpoint:
                        try:
                            u = psutil.disk_usage(p.mountpoint)
                            disk_total_dyn += int(u.total)
                            disk_used_dyn += int(u.used)
                        except Exception:
                            continue
                if disk_total_dyn > 0:
                    disk_pct = int(round(disk_used_dyn * 100.0 / disk_total_dyn))
                    data["sys_info"]["disk_usage_line"] = f"{bytes2human(disk_used_dyn)} ({disk_pct}%)"
                else:
                    data["sys_info"]["disk_usage_line"] = "N/A"
            except Exception:
                data["sys_info"]["disk_usage_line"] = "N/A"

            # 3) 显存（优先 LHM，其次 NVML，其次 Windows GPU 计数器）
            try:
                used_b = None
                total_b_dyn = None
                if metrics.lhm and metrics.lhm.ok:
                    u, t = metrics.lhm.gpu_mem_used_total_bytes()
                    used_b, total_b_dyn = u, t
                if used_b is None:
                    u2, t2 = metrics.nvml.gpu_mem_sum()
                    used_b = used_b or u2
                    total_b_dyn = total_b_dyn or t2
                if used_b is None:
                    used_b, _ = _try_gpu_adapter_memory_wmi_sum()
                denom = (sysinfo_once.get("vram_total") or total_b_dyn or 0)
                if used_b is not None and denom > 0:
                    used_clip = min(int(used_b), int(denom))
                    vram_pct = int(round(used_clip * 100.0 / int(denom)))
                    data["sys_info"]["vram_usage_line"] = f"{bytes2human(used_clip)} ({vram_pct}%)"
                else:
                    data["sys_info"]["vram_usage_line"] = "N/A"
            except Exception:
                data["sys_info"]["vram_usage_line"] = "N/A"

            # 写入 data.js
            try:
                write_data_js(args.out_dir, args.data_js_name, data, debug=args.debug)
                logging.info("所有数据写入到 data.js 成功")
            except Exception as e:
                logging.error(f"写入 data.js 失败: {e}")

            # 确保静态资源存在/按需覆盖
            try:
                ensure_files(
                    out_dir=args.out_dir,
                    html_name=args.html_name,
                    js_name=args.js_name,
                    overwrite=args.overwrite_assets,
                    debug=args.debug,
                    html_template_path=args.html_template,
                    js_template_path=args.js_template,
                    chart_js_path=args.chart_js,
                )
                logging.debug("静态资源校验/写入完成")
            except Exception as e:
                logging.error(f"写入静态资源失败: {e}")
        except Exception as e:
            logging.exception(f"采集/写入过程发生异常: {e}")
        finally:
            metrics.tick_time()
            elapsed = time.time() - loop_start
            to_sleep = max(0.0, args.interval - elapsed)
            time.sleep(to_sleep)

if __name__ == "__main__":
    # 管理员权限检查：未启用则 fatal，并提示按任意键退出
    if os.name == "nt":
        if not _is_admin():
            logging.critical("未以管理员权限运行，程序将退出。")
            try:
                ctypes.windll.user32.MessageBoxW(
                    None,
                    "需要以管理员权限运行本程序。\n按任意键退出...",
                    "权限不足",
                    0x00000010
                )
            except Exception:
                pass
            try:
                import msvcrt
                print("按任意键退出...", flush=True)
                msvcrt.getch()
            except Exception:
                try:
                    input("按回车键退出...")
                except Exception:
                    pass
            os._exit(1)
        else:
            logging.debug("已启用管理员模式")
    main()