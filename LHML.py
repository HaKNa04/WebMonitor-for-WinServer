import clr
import ctypes
import json
import logging
import os
import platform
import psutil
import re
import sys
import time
import winreg
from app_config import DEFAULT_LHM_CONFIG
from database import MariaDB
from nv_api import NVMLHelper
from typing import Optional, Tuple, List, Dict

try:
    import wmi as wmi_module
except Exception:
    wmi_module = None

def _get_file_version(path: str) -> Optional[str]:
    """
    仅使用 WinAPI 读取文件版本（不执行外部命令）
    """
    try:
        path_w = ctypes.c_wchar_p(path)
        size = ctypes.windll.version.GetFileVersionInfoSizeW(path_w, None)
        if not size:
            return None
        buf = ctypes.create_string_buffer(size)
        if not ctypes.windll.version.GetFileVersionInfoW(path_w, 0, size, buf):
            return None
        # 固定信息
        lptr = ctypes.c_void_p()
        lsize = ctypes.c_uint()
        if not ctypes.windll.version.VerQueryValueW(buf, ctypes.c_wchar_p("\\"), ctypes.byref(lptr), ctypes.byref(lsize)):
            return None

        class VS_FIXEDFILEINFO(ctypes.Structure):
            _fields_ = [
                ("dwSignature", ctypes.c_uint32),
                ("dwStrucVersion", ctypes.c_uint32),
                ("dwFileVersionMS", ctypes.c_uint32),
                ("dwFileVersionLS", ctypes.c_uint32),
                ("dwProductVersionMS", ctypes.c_uint32),
                ("dwProductVersionLS", ctypes.c_uint32),
                ("dwFileFlagsMask", ctypes.c_uint32),
                ("dwFileFlags", ctypes.c_uint32),
                ("dwFileOS", ctypes.c_uint32),
                ("dwFileType", ctypes.c_uint32),
                ("dwFileSubtype", ctypes.c_uint32),
                ("dwFileDateMS", ctypes.c_uint32),
                ("dwFileDateLS", ctypes.c_uint32),
            ]

        ffi = VS_FIXEDFILEINFO.from_address(lptr.value)
        ver = f"{ffi.dwFileVersionMS >> 16}.{ffi.dwFileVersionMS & 0xFFFF}.{ffi.dwFileVersionLS >> 16}.{ffi.dwFileVersionLS & 0xFFFF}"
        return ver
    except Exception:
        return None

def _detect_web_server_from_path(path_hint: Optional[str]) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    仅通过路径推断类型 & 读取文件版本；不执行任何外部命令。
    """
    resolved = None
    if path_hint:
        p = os.path.expandvars(os.path.expanduser(path_hint))
        if os.path.isdir(p):
            for candidate in ["nginx.exe", "nginx", "httpd.exe", "httpd", "apache2.exe", "apache2", "w3wp.exe", "appcmd.exe"]:
                cand = os.path.join(p, candidate)
                # 只接受真实文件，避免把同名目录误识别为可执行文件
                if os.path.isfile(cand):
                    resolved = cand
                    break
        elif os.path.isfile(p):
            resolved = p

    web_type: Optional[str] = None
    web_ver: Optional[str] = None

    def _parse_semver_from_text(txt: str) -> Optional[str]:
        # 从任意字符串里解析类似 1.28 或 1.28.0 或 2.4.57.1 的版本号
        m = re.search(r"(\d+(?:\.\d+){1,3})", txt or "", re.IGNORECASE)
        return m.group(1) if m else None

    if resolved:
        base = os.path.basename(resolved).lower()
        if "nginx" in base:
            web_type = "nginx"
        elif base in ("httpd", "httpd.exe") or "apache" in base:
            web_type = "Apache"
        elif base in ("w3wp.exe", "appcmd.exe"):
            web_type = "IIS"
        else:
            web_type = None
        # 首选：从文件版本资源读取
        web_ver = _get_file_version(resolved) if web_type else None
        # 回退：无法读取文件版本时，尝试从路径名/父目录名解析版本
        if web_type and not web_ver:
            try:
                parent_name = os.path.basename(os.path.dirname(resolved))
                web_ver = _parse_semver_from_text(parent_name) or _parse_semver_from_text(resolved)
            except Exception:
                pass

    # 目录名回退：未找到具体 exe，但目录名已能推断类型/版本
    if (not web_type) and path_hint:
        try:
            p = os.path.expandvars(os.path.expanduser(path_hint))
            base_dir = os.path.basename(os.path.normpath(p)).lower()
            if "nginx" in base_dir:
                web_type = "nginx"
                web_ver = _parse_semver_from_text(base_dir) or web_ver
            elif "apache" in base_dir or base_dir.startswith("httpd"):
                web_type = "Apache"
                web_ver = _parse_semver_from_text(base_dir) or web_ver
        except Exception:
            pass

    # IIS（可从注册表）
    if not web_type and winreg is not None and platform.system().lower() == "windows":
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\InetStp") as k:
                try:
                    maj, _ = winreg.QueryValueEx(k, "MajorVersion")
                    minv, _ = winreg.QueryValueEx(k, "MinorVersion")
                    web_type = "IIS"
                    web_ver = f"{int(maj)}.{int(minv)}"
                except Exception:
                    pass
        except Exception:
            pass

    return web_type, web_ver, resolved

def _read_java_version_from_registry() -> Optional[str]:
    if winreg is None or platform.system().lower() != "windows":
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\JavaSoft\Java Runtime Environment") as k:
            ver, _ = winreg.QueryValueEx(k, "CurrentVersion")
            if ver:
                return f"java version \"{ver}\""
    except Exception:
        pass
    return None


def _read_openssh_version_from_file() -> Optional[str]:
    if platform.system().lower() != "windows":
        return None
    root = os.environ.get("SystemRoot", r"C:\Windows")
    ssh_path = os.path.join(root, "System32", "OpenSSH", "ssh.exe")
    if os.path.exists(ssh_path):
        ver = _get_file_version(ssh_path)
        if ver:
            return f"OpenSSH_{ver}"
    return None

def _read_python_version_from_env_or_runtime() -> str:
    v = (os.environ.get("PYTHON_VERSION") or "").strip()
    if v:
        return v
    return platform.python_version()


def _read_java_version_from_env_or_registry() -> Optional[str]:
    v = (os.environ.get("JAVA_VERSION") or "").strip()
    if v:
        return f'java version "{v}"'
    home = (os.environ.get("JAVA_HOME") or "").strip()
    if home:
        cand = os.path.join(home, "bin", "java.exe")
        if os.path.exists(cand):
            fv = _get_file_version(cand)
            if fv:
                return f'java version "{fv}"'
    # 回退注册表
    return _read_java_version_from_registry()


def _parse_cuda_version_from_path(p: str) -> Optional[str]:
    # 例: C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.2 => 12.2
    m = re.search(r"[\\/]+v?(\d+(?:\.\d+){0,2})(?:[\\/]+|$)", p, re.IGNORECASE)
    return m.group(1) if m else None


def _read_cuda_version_from_env_or_nvml(nvml: NVMLHelper) -> Optional[str]:
    v = (os.environ.get("CUDA_VERSION") or "").strip()
    if v:
        # 常见形式可能是 "12.2" 或 "12.2.0"
        m = re.search(r"(\d+(?:\.\d+){0,2})", v)
        return m.group(1) if m else v
    # 查找最具体的 CUDA_PATH_V*，否则 CUDA_PATH
    keys = sorted([k for k in os.environ.keys() if k.upper().startswith("CUDA_PATH_V")], reverse=True)
    for k in keys + ["CUDA_PATH"]:
        pathv = os.environ.get(k)
        if not pathv:
            continue
        ver = _parse_cuda_version_from_path(pathv)
        if ver:
            return ver
    # 最后回退 NVML 驱动支持版本
    try:
        return nvml.cuda_driver_version_str()
    except Exception:
        return None


class LibreHMReader:
    """
    LibreHardwareMonitorLib.dll 读取器（不启动外部进程）。
    - 仅用相对路径尝试加载 DLL 名称（期望 DLL 与运行目录同级）；
    - 首次运行会在当前工作目录输出 LibreHM.dump.txt（相对路径）。
    """
    def __init__(self, debug: bool = False) -> None:
        self.debug = debug
        self.ok = False
        self._comp = None
        self._SensorType = None
        self._HardwareType = None
        self._dumped = False  # 仅输出一次快照
        if clr is None:
            if self.debug:
                logging.debug("pythonnet/clr 不可用，跳过 LibreHardwareMonitor")
            return
        try:
            # 只用相对路径尝试加载 DLL（要求 DLL 在当前工作目录）
            try:
                clr.AddReferenceToFileAndPath("LibreHardwareMonitorLib.dll")  # type: ignore
            except Exception:
                # 少数 pythonnet 版本需要回退
                clr.AddReference("LibreHardwareMonitorLib")  # type: ignore

            from LibreHardwareMonitor.Hardware import Computer, SensorType, HardwareType  # type: ignore
            self._SensorType = SensorType
            self._HardwareType = HardwareType

            comp = Computer()
            comp.IsCpuEnabled = True
            comp.IsMotherboardEnabled = True
            comp.IsControllerEnabled = True
            comp.IsMemoryEnabled = True
            comp.IsGpuEnabled = True
            comp.IsStorageEnabled = True
            comp.IsNetworkEnabled = True
            # 额外开启（有些环境下有助于加载底层驱动/节点）
            try:
                comp.IsPsuEnabled = True
                comp.IsBatteryEnabled = True
            except Exception:
                pass
            comp.Open()
            self._comp = comp
            self.ok = True
            # 启动自检：检查是否能看到 Nuvoton/EC 数值
            try:
                self._post_open_diagnose()
            except Exception:
                pass
            if self.debug:
                logging.debug("LibreHardwareMonitorLib 加载成功（相对路径）")
        except Exception as e:
            if self.debug:
                logging.debug(f"加载/初始化 LibreHardwareMonitorLib 失败: {e}")
            self.ok = False

    def _update_recursive(self, hw) -> None:
        try:
            hw.Update()
            for sub in hw.SubHardware:
                self._update_recursive(sub)
        except Exception:
            pass

    def _walk_collect(self, hw, indent: int, lines: List[str]) -> None:
        ind = "  " * indent
        try:
            lines.append(f"{ind}HW [{hw.HardwareType}] '{hw.Name or ''}' id={hw.Identifier}")
        except Exception:
            pass
        try:
            for s in hw.Sensors:
                try:
                    val = s.Value if s.Value is not None else "None"
                    vmin = s.Min if getattr(s, "Min", None) is not None else "None"
                    vmax = s.Max if getattr(s, "Max", None) is not None else "None"
                    lines.append(f"{ind}  - Sensor [{s.SensorType}] '{s.Name or ''}' id={s.Identifier} value={val} min={vmin} max={vmax}")
                except Exception:
                    continue
        except Exception:
            pass
        try:
            for sub in hw.SubHardware:
                self._walk_collect(sub, indent + 1, lines)
        except Exception:
            pass

    def debug_dump_once(self) -> None:
        """
        在当前工作目录创建 LibreHM.dump.txt（相对路径）。
        """
        # 禁用 LibreHM.dump.txt 输出，但保留函数接口
        return

        if self._dumped or (not self.ok) or (self._comp is None):
            return
        try:
            lines: List[str] = []
            for hw in self._comp.Hardware:
                self._update_recursive(hw)
                self._walk_collect(hw, 0, lines)
            with open("LibreHM.dump.txt", "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
            logging.info("LibreHardwareMonitor 传感器快照已写入: LibreHM.dump.txt")
        except Exception as e:
            logging.debug(f"输出 LibreHM.dump.txt 失败：{e}")
        finally:
            self._dumped = True

    def cpu_fan_rpm(self) -> Optional[float]:
        if not self.ok or self._comp is None:
            return None
        candidates: List[float] = []
        try:
            for hw in self._comp.Hardware:
                if hw.HardwareType == self._HardwareType.Motherboard:
                    self._update_recursive(hw)
                    for s in hw.Sensors:
                        if s.SensorType == self._SensorType.Fan:
                            name = (s.Name or "").lower()
                            if any(k in name for k in ("cpu", "aio", "pump")):
                                try:
                                    val = float(s.Value) if s.Value is not None else None
                                    if val is not None and 1.0 <= val < 20000.0:
                                        candidates.append(val)
                                except Exception:
                                    continue
                if hw.HardwareType == self._HardwareType.SuperIO or hw.HardwareType == self._HardwareType.Cooler:
                    self._update_recursive(hw)
                    for s in hw.Sensors:
                        if s.SensorType == self._SensorType.Fan:
                            name = (s.Name or "").lower()
                            if any(k in name for k in ("cpu", "aio", "pump")):
                                try:
                                    val = float(s.Value) if s.Value is not None else None
                                    if val is not None and 1.0 <= val < 20000.0:
                                        candidates.append(val)
                                except Exception:
                                    continue
        except Exception:
            pass
        return max(candidates) if candidates else None

    # ===== GPU 显存/网卡吞吐/磁盘吞吐（均来自 LHM） =====
    def gpu_mem_used_total_bytes(self) -> Tuple[Optional[int], Optional[int]]:
        """
        返回(used_bytes, total_bytes)，优先 SmallData:
        - 'GPU Memory Used' / 'GPU Memory Total'
        回退:
        - 'D3D Dedicated Memory Used' 作为 used
        多 GPU 聚合求和。单位 MB -> 转换为字节。
        """
        if not self.ok or self._comp is None:
            return None, None
        used = 0.0
        total = 0.0
        has_any = False
        try:
            for hw in self._comp.Hardware:
                hwt = str(hw.HardwareType)
                if "Gpu" not in hwt:
                    continue
                self._update_recursive(hw)

                u_local = None
                t_local = None
                d3d_ded_local = None

                for s in hw.Sensors:
                    try:
                        st = str(s.SensorType).lower()
                        name = (s.Name or "").lower()
                        ident = s.Identifier.ToString().lower() if hasattr(s.Identifier, "ToString") else str(s.Identifier).lower()

                        if "smalldata" in ident or st == "smalldata":
                            v = float(s.Value) if s.Value is not None else None
                            if v is None:
                                continue
                            if "gpu memory used" in name:
                                u_local = v
                            elif "gpu memory total" in name:
                                t_local = v
                            elif "d3d dedicated memory used" in name:
                                d3d_ded_local = v
                    except Exception:
                        continue

                # LHM 显存单位通常为 MB
                if u_local is None and d3d_ded_local is not None:
                    u_local = d3d_ded_local

                if u_local is not None:
                    used += u_local * 1_000_000.0
                    has_any = True
                if t_local is not None:
                    total += t_local * 1_000_000.0
                    has_any = True
        except Exception:
            return None, None

        if not has_any:
            return None, None
        return int(used) if used >= 0 else 0, int(total) if total > 0 else None

    def cpu_effective_freq_mhz(self) -> Optional[float]:
        """
        返回 CPU 当前频率（MHz），优先取各核心 'Core #n (Effective)' 的时钟，取平均。
        """
        if not self.ok or self._comp is None:
            return None
        vals: List[float] = []
        try:
            for hw in self._comp.Hardware:
                if hw.HardwareType == self._HardwareType.Cpu:
                    self._update_recursive(hw)
                    for s in hw.Sensors:
                        try:
                            if str(s.SensorType).lower() == "clock":
                                nm = (s.Name or "").lower()
                                # LHM 报表示例：Core #1 (Effective)/Core #2 (Effective)...
                                if "effective" in nm or nm.startswith("core #"):
                                    if s.Value is not None:
                                        v = float(s.Value)
                                        if v > 0:
                                            vals.append(v)
                        except Exception:
                            continue
        except Exception:
            pass
        if vals:
            return float(sum(vals) / len(vals))
        return None

    def cpu_package_temp_c(self) -> Optional[float]:
        """
        仅从 CPU 硬件节点中的温度传感器选取（package/tctl/tdie/ccd/die），不回退到 GPU。
        """
        if not self.ok or self._comp is None:
            return None
        try:
            candidates: List[float] = []
            for hw in self._comp.Hardware:
                if hw.HardwareType == self._HardwareType.Cpu:
                    self._update_recursive(hw)
                    for s in hw.Sensors:
                        if s.SensorType == self._SensorType.Temperature:
                            name = (s.Name or "").lower()
                            if any(k in name for k in ("package", "tctl", "tdie", "ccd", "die")):
                                try:
                                    val = float(s.Value) if s.Value is not None else None
                                    # 仅 CPU 节点内做基本合理性校验
                                    if val is not None and 0.0 <= val < 120.0:
                                        candidates.append(val)
                                except Exception:
                                    continue
            if candidates:
                return max(candidates)
        except Exception:
            pass
        return None

    def nic_up_down_bps(self) -> Tuple[Optional[int], Optional[int]]:
        """
        返回(上行bps, 下行bps)，聚合所有网卡的 Upload/Download Speed。
        LHM 的网卡吞吐传感器单位通常是 Byte/s，这里转为 bit/s。
        """
        if not self.ok or self._comp is None:
            return None, None
        up_bytes = 0.0
        down_bytes = 0.0
        has_any = False
        try:
            for hw in self._comp.Hardware:
                if str(hw.HardwareType).lower() != "network":
                    continue
                self._update_recursive(hw)
                u_local = 0.0
                d_local = 0.0
                for s in hw.Sensors:
                    try:
                        st = str(s.SensorType)
                        name = (s.Name or "").lower()
                        if st.lower() == "throughput":
                            if "upload" in name:
                                if s.Value is not None:
                                    u_local += float(s.Value)
                            elif "download" in name:
                                if s.Value is not None:
                                    d_local += float(s.Value)
                    except Exception:
                        continue
                if u_local > 0 or d_local > 0:
                    has_any = True
                    up_bytes += u_local
                    down_bytes += d_local
        except Exception:
            return None, None
        if not has_any:
            return None, None
        return int(up_bytes * 8.0), int(down_bytes * 8.0)

    def storage_read_write_bps(self) -> Tuple[Optional[int], Optional[int]]:
        """
        返回(读bps, 写bps)，聚合所有磁盘的 Read/Write Rate。
        LHM 的磁盘吞吐传感器单位通常是 Byte/s，这里转为 bit/s。
        """
        if not self.ok or self._comp is None:
            return None, None
        r_bytes = 0.0
        w_bytes = 0.0
        has_any = False
        try:
            for hw in self._comp.Hardware:
                # Storage / HDD / NVMe 在不同版本里 HardwareType 可能不同，这里放宽匹配
                hwt = str(hw.HardwareType).lower()
                if not any(k in hwt for k in ("storage", "hdd", "nvme")):
                    if not any(tag in (hw.Name or "").lower() for tag in ("nvme", "hdd", "ssd", "wdc", "st")):
                        continue
                self._update_recursive(hw)
                r_local = 0.0
                w_local = 0.0
                for s in hw.Sensors:
                    try:
                        st = str(s.SensorType).lower()
                        name = (s.Name or "").lower()
                        if st == "throughput":
                            if "read rate" in name or ("read" in name and "rate" in name):
                                if s.Value is not None:
                                    r_local += float(s.Value)
                            elif "write rate" in name or ("write" in name and "rate" in name):
                                if s.Value is not None:
                                    w_local += float(s.Value)
                    except Exception:
                        continue
                if r_local > 0 or w_local > 0:
                    has_any = True
                    r_bytes += r_local
                    w_bytes += w_local
        except Exception:
            return None, None
        if not has_any:
            return None, None
        return int(r_bytes * 8.0), int(w_bytes * 8.0)

    def _post_open_diagnose(self) -> None:
        if not (self.ok and self._comp):
            return

        def walk(hw):
            yield hw
            try:
                for sub in hw.SubHardware:
                    yield from walk(sub)
            except Exception:
                return

        has_superio = False
        superio_temps: List[Tuple[str, float]] = []
        ec_temps: List[Tuple[str, float]] = []

        try:
            for hw in self._comp.Hardware:
                # 递归更新整棵树
                self._update_recursive(hw)
                for node in walk(hw):
                    try:
                        hwt = str(node.HardwareType).lower()
                        ident = str(getattr(node, "Identifier", "")).lower()
                    except Exception:
                        hwt = ""
                        ident = ""

                    # 通用 SuperIO 检测（不再依赖厂商名）
                    if self._HardwareType and getattr(self, "_HardwareType", None):
                        try:
                            if node.HardwareType == self._HardwareType.SuperIO:
                                has_superio = True
                        except Exception:
                            pass
                    # 兜底：字符串匹配
                    if "superio" in hwt:
                        has_superio = True

                    # 采集 SuperIO 与 EC 的温度
                    try:
                        for s in getattr(node, "Sensors", []):
                            if s.SensorType == self._SensorType.Temperature:
                                sid = s.Identifier.ToString() if hasattr(s.Identifier, "ToString") else str(s.Identifier)
                                v = float(s.Value) if s.Value is not None else None
                                if v is None:
                                    continue
                                if ("superio" in hwt):
                                    superio_temps.append((sid, v))
                                elif ("embeddedcontroller" in hwt) or ("/lpc/ec/" in ident):
                                    ec_temps.append((sid, v))
                    except Exception:
                        continue
        except Exception:
            pass

        # 打印调试信息
        if self.debug:
            logging.debug(f"LHM 诊断: SuperIO={has_superio}, SuperIO_temps={len(superio_temps)}, EC_temps={len(ec_temps)}")

        def all_zero(arr: List[Tuple[str, float]]) -> bool:
            return bool(arr) and all(abs(v) < 1e-6 for _, v in arr)

        if not has_superio:
            logging.warning(
                "未检测到 SuperIO 节点。可能原因：未以管理员权限运行、LHM 内核驱动未加载、或 DLL 版本与 GUI 不一致。"
                " 建议：以管理员权限运行 Python；确保 LibreHardwareMonitorLib.dll 与 GUI 报告版本一致（如 0.9.4.0）；关闭可能占用 EC/SuperIO 的其他监控软件后重试。"
            )
        elif all_zero(superio_temps):
            logging.warning(
                "检测到 SuperIO，但温度值均为 0。可能是底层访问受限或冲突。"
                " 建议：以管理员权限运行；确认无其他软件独占 EC/SuperIO；确保 DLL 与 GUI 版本一致。"
            )
        elif all_zero(ec_temps) and ec_temps:
            logging.info(
                "EmbeddedController 温度为 0，但 SuperIO 正常。在部分主板上属正常，优先选择 SuperIO 下的温度（例如 /lpc/.../temperature/1）。"
            )

class SensorSelector:
    """
    负责：
    - 递归列举所有可用的温度/风扇来源（LHM 包括子硬件 + WMI）
    - 读取/写入 LibreHM.config
    - 启动时若无配置：先打印温度候选并等待选择，再打印风扇候选并等待选择；无 TTY 时按启发式自动选择
    - 运行时按配置读取数值（选定来源将不再二次过滤）
    """
    def __init__(self, lhm: Optional[LibreHMReader], debug: bool = False, config_path: str = DEFAULT_LHM_CONFIG) -> None:
        self.lhm = lhm
        self.debug = debug
        self.config_path = config_path
        self.sel_temp: Optional[Dict] = None
        self.sel_fan: Optional[Dict] = None

    def _iter_lhm_sensors_recursive(self):
        if not (self.lhm and self.lhm.ok and self.lhm._comp):
            return
        def walk(hw):
            yield hw
            try:
                for sub in hw.SubHardware:
                    yield from walk(sub)
            except Exception:
                return
        try:
            for hw in self.lhm._comp.Hardware:
                for node in walk(hw):
                    try:
                        self.lhm._update_recursive(node)
                        for s in node.Sensors:
                            yield node, s
                    except Exception:
                        continue
        except Exception:
            return

    def list_temp_candidates(self) -> List[Dict]:
        cands: List[Dict] = []
        # LHM 温度（递归所有子硬件）
        try:
            for hw, s in self._iter_lhm_sensors_recursive() or []:
                try:
                    if s.SensorType == self.lhm._SensorType.Temperature:  # type: ignore
                        ident = s.Identifier.ToString() if hasattr(s.Identifier, "ToString") else str(s.Identifier)
                        name = f"LHM | {hw.HardwareType} | {(hw.Name or '')} | {(s.Name or '')}"
                        val = None
                        try:
                            if s.Value is not None:
                                val = float(s.Value)
                        except Exception:
                            val = None
                        cands.append({"type": "LHM", "id": ident, "name": name, "value": val})
                except Exception:
                    continue
        except Exception:
            pass
        # WMI 温度（ACPI ThermalZone）
        if wmi_module is not None and platform.system().lower() == "windows":
            try:
                c = wmi_module.WMI(namespace="root\\wmi")
                items = c.MSAcpi_ThermalZoneTemperature()
                for i, it in enumerate(items):
                    t = getattr(it, "CurrentTemperature", None)
                    if t is None:
                        t = getattr(it, "Temperature", None)
                    val = None
                    try:
                        if t is not None:
                            val = float(t) / 10.0 - 273.15
                    except Exception:
                        pass
                    name = f"WMI | ACPI ThermalZone | idx={i} | {getattr(it,'InstanceName', '')}"
                    cands.append({"type": "WMI_ACPI", "id": f"WMI:ACPI:{i}", "name": name, "value": val})
            except Exception:
                pass
        return cands

    def list_fan_candidates(self) -> List[Dict]:
        cands: List[Dict] = []
        # LHM 风扇（递归所有子硬件）
        try:
            for hw, s in self._iter_lhm_sensors_recursive() or []:
                try:
                    if s.SensorType == self.lhm._SensorType.Fan:  # type: ignore
                        ident = s.Identifier.ToString() if hasattr(s.Identifier, "ToString") else str(s.Identifier)
                        name = f"LHM | {hw.HardwareType} | {(hw.Name or '')} | {(s.Name or '')}"
                        val = None
                        try:
                            if s.Value is not None:
                                val = float(s.Value)
                        except Exception:
                            val = None
                        cands.append({"type": "LHM", "id": ident, "name": name, "value": val})
                except Exception:
                    continue
        except Exception:
            pass
        # WMI 风扇
        if wmi_module is not None and platform.system().lower() == "windows":
            try:
                c = wmi_module.WMI()
                items = c.Win32_Fan()
                for i, it in enumerate(items):
                    sp = getattr(it, "Speed", None)
                    if sp is None:
                        sp = getattr(it, "DesiredSpeed", None)
                    val = None
                    try:
                        if sp is not None:
                            val = float(sp)
                    except Exception:
                        pass
                    name = f"WMI | Win32_Fan | idx={i} | {getattr(it,'Name','')} {getattr(it,'DeviceID','')}"
                    cands.append({"type": "WMI_FAN", "id": f"WMI:FAN:{i}", "name": name, "value": val})
            except Exception:
                pass
        return cands

    def _auto_pick_temp(self, temps: List[Dict]) -> Optional[Dict]:
        # 启发式：优先名称含 package/tctl/tdie/cpu 的 LHM 候选，且数值在(0.5,120)；其次 WMI ACPI
        def okv(v): 
            try:
                vv = float(v)
                return 0.5 < vv < 120.0
            except Exception:
                return False
        cpu_keywords = ("package", "tctl", "tdie", "cpu")
        cpu_like = [t for t in temps if t["type"] == "LHM" and any(k in (t["name"] or "").lower() for k in cpu_keywords) and okv(t.get("value"))]
        if cpu_like:
            return max(cpu_like, key=lambda x: float(x["value"]))
        acpi = [t for t in temps if t["type"] == "WMI_ACPI" and okv(t.get("value"))]
        if acpi:
            return max(acpi, key=lambda x: float(x["value"]))
        return None

    def _auto_pick_fan(self, fans: List[Dict]) -> Optional[Dict]:
        def okv(v): 
            try:
                vv=float(v); 
                return 1.0 <= vv < 20000.0
            except Exception: 
                return False
        pri = [f for f in fans if f["type"] == "LHM" and any(k in (f["name"] or "").lower() for k in ("cpu","aio","pump")) and okv(f.get("value"))]
        if pri:
            return max(pri, key=lambda x: float(x["value"]))
        lhm_any = [f for f in fans if f["type"] == "LHM" and okv(f.get("value"))]
        if lhm_any:
            return max(lhm_any, key=lambda x: float(x["value"]))
        wmi_fans = [f for f in fans if f["type"] == "WMI_FAN" and okv(f.get("value"))]
        if wmi_fans:
            return max(wmi_fans, key=lambda x: float(x["value"]))
        return None

    def _save_config(self):
        cfg = {"temp_source": self.sel_temp, "fan_source": self.sel_fan, "created_at": time.strftime("%Y-%m-%d %H:%M:%S")}
        try:
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            logging.info(f"传感器来源配置已写入: {self.config_path}")
        except Exception as e:
            logging.warning(f"写入 {self.config_path} 失败: {e}")

    def _load_config(self) -> bool:
        if not os.path.exists(self.config_path):
            return False
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            self.sel_temp = cfg.get("temp_source") or None
            self.sel_fan = cfg.get("fan_source") or None
            return True
        except Exception as e:
            logging.warning(f"读取 {self.config_path} 失败: {e}")
            return False

    def _print_lines(self, title: str, lines: List[str], tty: bool):
        if tty:
            print(title, flush=True)
            for s in lines:
                print(s, flush=True)
        else:
            logging.info(title)
            for s in lines:
                logging.info(s)

    def ensure_selection(self):
        # 已有配置直接返回（不等待）
        if self._load_config():
            return

        is_tty = bool(sys.stdin and sys.stdin.isatty())

        # 1) 温度：列出并阻塞选择
        temps = self.list_temp_candidates()
        temp_lines = [f"  [{idx}] {t['name']} | id={t['id']} | 当前值={t.get('value')}" for idx, t in enumerate(temps)]
        self._print_lines("温度候选源：", temp_lines, is_tty)

        self.sel_temp = None
        if is_tty and temps:
            while True:
                tin = input("请选择温度来源索引（回车自动选择）：").strip()
                if tin == "":
                    break
                if tin.isdigit() and 0 <= int(tin) < len(temps):
                    self.sel_temp = temps[int(tin)]
                    break
                print("输入无效，请重试。", flush=True)
        if self.sel_temp is None:
            self.sel_temp = self._auto_pick_temp(temps)
            if not is_tty:
                logging.info(f"自动选择温度源: {self.sel_temp['name'] if self.sel_temp else '无'}")

        # 2) 风扇：列出并阻塞选择
        fans = self.list_fan_candidates()
        fan_lines = [f"  [{idx}] {f['name']} | id={f['id']} | 当前值={f.get('value')}" for idx, f in enumerate(fans)]
        self._print_lines("风扇候选源：", fan_lines, is_tty)

        self.sel_fan = None
        if is_tty and fans:
            while True:
                fin = input("请选择风扇来源索引（回车自动选择）：").strip()
                if fin == "":
                    break
                if fin.isdigit() and 0 <= int(fin) < len(fans):
                    self.sel_fan = fans[int(fin)]
                    break
                print("输入无效，请重试。", flush=True)
        if self.sel_fan is None:
            self.sel_fan = self._auto_pick_fan(fans)
            if not is_tty:
                logging.info(f"自动选择风扇源: {self.sel_fan['name'] if self.sel_fan else '无'}")

        self._save_config()

    def _read_lhm_by_id(self, ident: str) -> Optional[float]:
        if not (self.lhm and self.lhm.ok and self.lhm._comp):
            return None
        try:
            for hw, s in self._iter_lhm_sensors_recursive() or []:
                sid = s.Identifier.ToString() if hasattr(s.Identifier, "ToString") else str(s.Identifier)
                if sid == ident:
                    try:
                        return float(s.Value) if s.Value is not None else None
                    except Exception:
                        return None
        except Exception:
            return None
        return None

    def read_temp_c(self) -> Optional[float]:
        # 按用户选择的来源读取值（不强制过滤 0 值）
        if self.sel_temp is None:
            return None
        try:
            if self.sel_temp["type"] == "LHM":
                return self._read_lhm_by_id(self.sel_temp["id"])
            if self.sel_temp["type"] == "WMI_ACPI" and wmi_module is not None and platform.system().lower() == "windows":
                idx = int(self.sel_temp["id"].split(":")[-1])
                c = wmi_module.WMI(namespace="root\\wmi")
                items = c.MSAcpi_ThermalZoneTemperature()
                if 0 <= idx < len(items):
                    t = getattr(items[idx], "CurrentTemperature", None)
                    if t is None:
                        t = getattr(items[idx], "Temperature", None)
                    if t is not None:
                        return float(t) / 10.0 - 273.15
        except Exception:
            return None
        return None

    def read_fan_rpm(self) -> Optional[float]:
        # 按用户选择的来源读取值（不强制过滤 0 值）
        if self.sel_fan is None:
            return None
        try:
            if self.sel_fan["type"] == "LHM":
                return self._read_lhm_by_id(self.sel_fan["id"])
            if self.sel_fan["type"] == "WMI_FAN" and wmi_module is not None and platform.system().lower() == "windows":
                idx = int(self.sel_fan["id"].split(":")[-1])
                c = wmi_module.WMI()
                items = c.Win32_Fan()
                if 0 <= idx < len(items):
                    sp = getattr(items[idx], "Speed", None)
                    if sp is None:
                        sp = getattr(items[idx], "DesiredSpeed", None)
                    if sp is not None:
                        return float(sp)
        except Exception:
            return None
        return None

    def _prefetch_refresh(self, label: str, tries: int = 5, interval: float = 2.0, tty: bool = False) -> None:
        """
        在交互前进行多次刷新，避免一次性读取到 0。
        - label: "温度"/"风扇" 等提示标签
        - tries: 刷新次数
        - interval: 每次刷新间隔秒
        - tty: 是否有交互终端；有则用 print，无则 logging.info
        """
        # 已移除预刷新逻辑
        return

        if not (self.lhm and self.lhm.ok and self.lhm._comp):
            return
        for i in range(tries):
            msg = f"[{label}] 正在刷新 {i+1}/{tries}（间隔 {interval:.0f}s）..."
            if tty:
                print(msg, flush=True)
            else:
                logging.info(msg)
            try:
                # 刷新所有硬件节点（包含子硬件）
                for hw in self.lhm._comp.Hardware:
                    self.lhm._update_recursive(hw)
            except Exception:
                pass
            time.sleep(interval)

class Metrics:
    def __init__(self, process_names: List[str], debug: bool = False) -> None:
        self.process_names = [p.strip() for p in process_names if p.strip()]
        self.prev_net = psutil.net_io_counters()
        self.prev_disks = psutil.disk_io_counters(perdisk=True)
        self.prev_time = time.time()
        self.debug = debug

        # 逻辑 CPU 数缓存
        self.ncpu = max(1, int(psutil.cpu_count(logical=True) or os.cpu_count() or 1))
        if self.debug:
            logging.debug(f"[Metrics] 逻辑 CPU 线程数: {self.ncpu}")

        # 解析“显示名=路径子串” -> [(display, pattern_lower)]
        self.process_queries: List[Tuple[str, str]] = self._build_process_queries(self.process_names)
        if self.debug:
            logging.debug(f"[Metrics] 进程查询项: {self.process_queries}")

        self.nvml = NVMLHelper(debug=debug)
        self.lhm = LibreHMReader(debug=debug)
        self.selector = SensorSelector(self.lhm, debug=debug, config_path=DEFAULT_LHM_CONFIG)
        try:
            self.selector.ensure_selection()
        except Exception as e:
            logging.warning(f"初始化传感器来源失败，将使用自动回退: {e}")
        if debug == debug:
            try:
                if self.lhm and self.lhm.ok:
                    self.lhm.debug_dump_once()
            except Exception:
                pass

        psutil.cpu_percent(interval=0.1, percpu=True)

    def _build_process_queries(self, items: List[str]) -> List[Tuple[str, str]]:
        """
        将 ['显示名=路径子串', 'X', ...] 解析为 [(显示名, 路径子串lower)]。
        若没有 '='，则显示名与路径子串相同。
        """
        queries: List[Tuple[str, str]] = []
        for raw in items:
            s = (raw or "").strip()
            if not s:
                continue
            if "=" in s:
                disp, pat = s.split("=", 1)
            else:
                disp, pat = s, s
            disp = (disp or "").strip()
            pat = (pat or "").strip().lower()
            if not disp or not pat:
                continue
            queries.append((disp, pat))
        return queries

    def collect_cpu(self) -> Tuple[float, List[float]]:
        total = psutil.cpu_percent(interval=None)
        per_core = psutil.cpu_percent(interval=None, percpu=True)
        return float(total), [float(x) for x in per_core]

    def _cpu_fan_rpm_wmi(self) -> Optional[float]:
        if wmi_module is None or platform.system().lower() != "windows":
            return None
        try:
            c = wmi_module.WMI()
            fans = c.Win32_Fan()  # 可能无数据
            vals: List[float] = []
            for f in fans:
                sp = getattr(f, "Speed", None)
                if sp is None:
                    sp = getattr(f, "DesiredSpeed", None)
                if sp is not None:
                    try:
                        rpm = float(sp)
                        if 1.0 <= rpm < 20000.0:
                            vals.append(rpm)
                    except Exception:
                        pass
            if vals:
                return sum(vals) / len(vals)
        except Exception:
            pass
        return None

    def _cpu_package_temp_wmi(self) -> Optional[float]:
        # 使用 ACPI ThermalZone（单位 1/10 K），并取最大值作为近似 CPU 区域温度
        if wmi_module is None or platform.system().lower() != "windows":
            return None
        try:
            c = wmi_module.WMI(namespace="root\\wmi")
            try:
                items = c.MSAcpi_ThermalZoneTemperature()
            except Exception:
                items = []
            vals: List[float] = []
            for it in items:
                t = getattr(it, "CurrentTemperature", None)
                if t is None:
                    t = getattr(it, "Temperature", None)
                if t is not None:
                    try:
                        celsius = float(t) / 10.0 - 273.15
                        if -20.0 < celsius < 120.0 and celsius > 0.5:
                            vals.append(celsius)
                    except Exception:
                        pass
            if vals:
                return max(vals)
        except Exception:
            pass
        return None

    def cpu_stats(self) -> Dict[str, Optional[float]]:
        # 频率：优先 LHM（各核心 Effective 时钟平均），回退 psutil
        freq_mhz: Optional[float] = None
        try:
            if self.lhm and self.lhm.ok:
                freq_mhz = self.lhm.cpu_effective_freq_mhz()
        except Exception:
            pass
        if freq_mhz is None:
            try:
                freq = psutil.cpu_freq()
                freq_mhz = float(freq.current) if freq else None
            except Exception:
                freq_mhz = None

        # 温度/风扇：优先配置选定来源；失败回退 LHM -> WMI
        fan_rpm = None
        package_temp_c = None
        try:
            if self.selector:
                package_temp_c = self.selector.read_temp_c()
                fan_rpm = self.selector.read_fan_rpm()
        except Exception:
            pass

        # 回退：LHM
        try:
            if package_temp_c is None and self.lhm and self.lhm.ok:
                package_temp_c = self.lhm.cpu_package_temp_c()
            if fan_rpm is None and self.lhm and self.lhm.ok:
                fan_rpm = self.lhm.cpu_fan_rpm()
        except Exception:
            pass

        # 回退：WMI
        if fan_rpm is None:
            fan_rpm = self._cpu_fan_rpm_wmi()
        if package_temp_c is None:
            package_temp_c = self._cpu_package_temp_wmi()

        return {"freq_mhz": freq_mhz, "fan_rpm": fan_rpm, "package_temp_c": package_temp_c}
    
    def net_io_rates(self) -> Dict[str, int]:
        try:
            if self.lhm and self.lhm.ok:
                up_bps, down_bps = self.lhm.nic_up_down_bps()
                if up_bps is not None and down_bps is not None:
                    if self.debug:
                        logging.debug(f"net(LHM) up={up_bps}bps down={down_bps}bps")
                    return {"up_bps": int(up_bps), "down_bps": int(down_bps)}
        except Exception:
            pass
        now = time.time()
        delta = max(0.001, now - self.prev_time)
        current = psutil.net_io_counters()
        up_bps = int((current.bytes_sent - self.prev_net.bytes_sent) * 8 / delta)
        down_bps = int((current.bytes_recv - self.prev_net.bytes_recv) * 8 / delta)
        self.prev_net = current
        if self.debug:
            logging.debug(f"net(psutil) delta={delta:.3f}s up={up_bps}bps down={down_bps}bps")
        return {"up_bps": up_bps, "down_bps": down_bps}

    def disk_io_rates(self) -> Dict[str, int]:
        # 优先 LHM（聚合所有磁盘 Read/Write Rate -> bit/s）
        try:
            if self.lhm and self.lhm.ok:
                r_bps, w_bps = self.lhm.storage_read_write_bps()
                if r_bps is not None and w_bps is not None:
                    if self.debug:
                        logging.info(f"disk(LHM) read={r_bps}bps write={w_bps}bps")
                    return {"read_bps": int(r_bps), "write_bps": int(w_bps)}
        except Exception:
            pass
        # 回退 psutil 增量法
        now_disks = psutil.disk_io_counters(perdisk=True)
        read_bytes = 0
        write_bytes = 0
        for name, stats in now_disks.items():
            prev = self.prev_disks.get(name)
            if prev:
                read_bytes += max(0, stats.read_bytes - prev.read_bytes)
                write_bytes += max(0, stats.write_bytes - prev.write_bytes)
        self.prev_disks = now_disks
        delta = max(0.001, time.time() - self.prev_time)
        read_bps = int(read_bytes * 8 / delta)
        write_bps = int(write_bytes * 8 / delta)
        if self.debug:
            logging.debug(f"disk(psutil) delta={delta:.3f}s read={read_bps}bps write={write_bps}bps")
        return {"read_bps": read_bps, "write_bps": write_bps}

    def system_info(self) -> Dict[str, Optional[int]]:
        os_ver = f"{platform.system()} {platform.release()} ({platform.version()})"
        cpu_model = None
        try:
            import wmi as wmi2  # type: ignore
            c = wmi2.WMI()
            cpus = c.Win32_Processor()
            if cpus:
                cpu_model = cpus[0].Name
        except Exception:
            cpu_model = platform.processor() or None
        ram_total = int(psutil.virtual_memory().total)

        # VRAM total：优先 LHM，其次 NVML，再次 WMI
        vram_total = None
        try:
            if self.lhm and self.lhm.ok:
                _, total_b = self.lhm.gpu_mem_used_total_bytes()
                if total_b:
                    vram_total = int(total_b)
        except Exception:
            vram_total = None

        if vram_total is None:
            try:
                _, total_b = self.nvml.gpu_mem_sum()
                if total_b:
                    vram_total = int(total_b)
            except Exception:
                vram_total = None

        if vram_total is None:
            try:
                import wmi as wmi3  # type: ignore
                c = wmi3.WMI()
                total = 0
                for vc in c.Win32_VideoController():
                    try:
                        if vc.AdapterRAM:
                            total += int(vc.AdapterRAM)
                    except Exception:
                        continue
                vram_total = total if total > 0 else None
            except Exception:
                vram_total = None

        disk_total = 0
        for p in psutil.disk_partitions(all=False):
            if p.fstype and p.mountpoint:
                try:
                    usage = psutil.disk_usage(p.mountpoint)
                    disk_total += int(usage.total)
                except Exception:
                    continue
        return {
            "os_version": os_ver,
            "cpu_model": cpu_model,
            "ram_total": ram_total,
            "vram_total": vram_total,
            "disk_total": disk_total,
        }

    def sw_versions(self, db: Optional["MariaDB"] = None, web_server_path: Optional[str] = None) -> Dict[str, Optional[str]]:
        def _extract_version_num(s: Optional[str]) -> Optional[str]:
            if not s:
                return None
            m = re.search(r"\b(\d+(?:\.\d+){0,3})\b", s)
            return m.group(1) if m else None

        # Web 服务器版本（仅路径/注册表 + 文件版本）
        web_type, web_ver, web_path_resolved = _detect_web_server_from_path(web_server_path or None)
        web_server_display = None
        if web_type and web_ver:
            web_server_display = f"{web_type} {web_ver}"
        elif web_type:
            web_server_display = f"{web_type} N/A"

        # Java 版本（环境变量优先 -> JAVA_HOME 文件版本 -> 注册表）
        java = _read_java_version_from_env_or_registry()

        # Python & CUDA（环境变量优先；CUDA 无则回退 NVML）
        py_ver_str = _read_python_version_from_env_or_runtime()
        cuda_ver_str = _read_cuda_version_from_env_or_nvml(self.nvml)

        # 数据库类型/版本（仅通过 DB 连接查询；不执行 mysql/mariadb 命令）
        def _classify_mysql_family(ver: Optional[str], comment: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
            txt = f"{ver or ''} {comment or ''}".lower()
            if "mariadb" in txt:
                return "MariaDB", _extract_version_num(ver)
            if "mysql" in txt or "percona" in txt or ver:
                return "MySQL", _extract_version_num(ver)
            return None, _extract_version_num(ver)

        db_type: Optional[str] = None
        db_version: Optional[str] = None
        if db is not None:
            try:
                rows = db.query("SELECT VERSION() AS ver")
                ver = (rows[0].get("ver") if rows else None) or None
                comment = None
                try:
                    rows2 = db.query("SELECT @@version_comment AS vc")
                    comment = (rows2[0].get("vc") if rows2 else None) or None
                except Exception:
                    comment = None
                db_type, db_version = _classify_mysql_family(ver, comment)
            except Exception:
                db_type = None
                db_version = None

        # OpenSSH（Windows 内置）尝试通过文件版本
        openssh = _read_openssh_version_from_file()

        python_cuda = f"Python {py_ver_str} | CUDA {cuda_ver_str or 'N/A'}"

        return {
            "web_server": web_server_display or "N/A",
            "web_server_type": web_type or "N/A",
            "web_server_version": web_ver or "N/A",
            "web_server_path": web_path_resolved or (web_server_path or "N/A"),
            "aida64": "N/A",            # HWiNFO/AIDA64 已移除
            "aida64_path": "N/A",
            "nginx": web_server_display or "N/A",
            "java": java or "N/A",
            "python_cuda": python_cuda,
            "python": f"Python {py_ver_str}",
            "cuda": f"CUDA {cuda_ver_str}" if cuda_ver_str else "CUDA N/A",
            "db_type": db_type or "N/A",
            "db_version": db_version or "N/A",
            "openssh": openssh or "N/A",
        }

    def process_status(self) -> List[Dict]:
        """
        进程检测（路径匹配版）：
        - 仅使用进程可执行文件完整路径 exe 做大小写不敏感的连续子串匹配；
        - 查询项格式：显示名=路径子串（显示名用于展示/入库，不参与匹配）；
        - 父路径会匹配其下所有进程；
        - 防爆阈值：每项匹配数>64 记录警告；>128 立即终止并仅返回该项的告警结果。
        CPU%：聚合后按逻辑 CPU 数归一化为“整机 100% 尺度”
        """
        queries = getattr(self, "process_queries", None) or self._build_process_queries(self.process_names)
        if not queries:
            return []

        grouped: Dict[str, Dict[str, float]] = {}
        match_samples: Dict[str, List[str]] = {}
        match_counts: Dict[str, int] = {}
        warn_flags: Dict[str, bool] = {}
        hard_abort_item: Optional[Tuple[str, str]] = None  # (display, pattern)

        ncpu = getattr(self, "ncpu", None) or max(1, int(psutil.cpu_count(logical=True) or os.cpu_count() or 1))

        for proc in psutil.process_iter(attrs=["name", "exe"]):
            if hard_abort_item:
                break
            try:
                pname = proc.info.get("name") or ""
                pexe = (proc.info.get("exe") or "").strip()
                if not pexe:
                    continue
                pexe_l = pexe.lower()

                for disp, pat in queries:
                    if not pat:
                        continue
                    if pat in pexe_l:
                        # 初始化聚合项
                        if disp not in grouped:
                            grouped[disp] = {"instances": 0, "cpu_raw": 0.0, "mem": 0.0}
                            match_samples[disp] = []
                            match_counts[disp] = 0
                            warn_flags[disp] = False

                        grouped[disp]["instances"] += 1
                        match_counts[disp] += 1

                        if len(match_samples[disp]) < 5:
                            match_samples[disp].append(pexe)

                        try:
                            grouped[disp]["cpu_raw"] += float(proc.cpu_percent(interval=None))
                        except Exception:
                            pass
                        try:
                            grouped[disp]["mem"] += float(proc.memory_info().rss)
                        except Exception:
                            pass

                        cnt = match_counts[disp]
                        if cnt > 64 and not warn_flags[disp]:
                            warn_flags[disp] = True
                            logging.warning(
                                f"进程搜索项匹配过多(>{64})：显示名='{disp}', pattern='{pat}', 当前匹配={cnt}，示例：{match_samples[disp]}"
                            )
                        if cnt > 128:
                            hard_abort_item = (disp, pat)
                            logging.error(
                                f"进程搜索项匹配过多(>{128})，已终止本次检测：显示名='{disp}', pattern='{pat}', 当前匹配={cnt}，示例：{match_samples[disp]}"
                            )
                        break  # 一个进程只归入首个命中的查询项
            except Exception:
                continue

        result: List[Dict] = []

        if hard_abort_item:
            disp, pat = hard_abort_item
            agg = grouped.get(disp, {"instances": 0, "cpu_raw": 0.0, "mem": 0.0})
            cnt = match_counts.get(disp, 0)
            name_display = f"{disp} [匹配过多({cnt}) 已终止]"
            cpu_norm = float(agg["cpu_raw"]) / float(ncpu) if ncpu > 0 else 0.0
            result.append(
                {
                    "name": name_display,
                    "instances": int(agg["instances"]),
                    "cpu_percent": float(cpu_norm),
                    "mem_rss": int(agg["mem"]),
                }
            )
            return result

        for disp, pat in queries:
            agg = grouped.get(disp, {"instances": 0, "cpu_raw": 0.0, "mem": 0.0})
            cnt = match_counts.get(disp, 0)
            name_display = disp
            if warn_flags.get(disp, False):
                name_display = f"{disp} [匹配过多({cnt})]"
            cpu_norm = float(agg["cpu_raw"]) / float(ncpu) if ncpu > 0 else 0.0
            if cnt > 0 and self.debug:
                logging.debug(
                    f"proc match 显示名='{disp}', pattern='{pat}': instances={int(agg['instances'])}, "
                    f"cpu_raw_sum={float(agg['cpu_raw']):.2f}%, cpu_norm={cpu_norm:.2f}%, mem_sum={int(agg['mem'])}B, ncpu={ncpu}, samples={match_samples.get(disp, [])}"
                )
            result.append(
                {
                    "name": name_display,
                    "instances": int(agg["instances"]),
                    "cpu_percent": float(cpu_norm),
                    "mem_rss": int(agg["mem"]),
                }
            )
        return result

    def tick_time(self):
        self.prev_time = time.time()
