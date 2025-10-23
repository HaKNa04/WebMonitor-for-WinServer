
    (function () {
        let LATEST = null;
        let CHART = null;

        function colorByPct(p, thresholds) {
            const [y1, y2, y3] = thresholds || [50, 80, 95];
            if (p < y1) return "#22c55e";
            if (p < y2) return "#eab308";
            if (p < y3) return "#f97316";
            return "#ef4444";
        }

        // 基础工具
        function escapeHtml(s) {
            return String(s ?? "").replace(/[&<>"']/g, m => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[m]));
        }
        function escapeAttr(s) {
            return String(s ?? "").replace(/["'<>]/g, m => ({ '"': '&quot;', "'": '&#39;', '<': '&lt;', '>': '&gt;' }[m]));
        }
        function normalizeUrl(u) {
            let s = String(u || "").trim();
            if (!s) return null;
            if (/^(https?:|file:|data:|ftp:)/i.test(s)) return s;
            if (s.startsWith("//")) return (location.protocol || "https:") + s;
            if (/^www\./i.test(s)) return "https://" + s;
            // 其余视为相对路径（例如 123.html）
            return s;
        }

        function renderChart(d) {
            const el = document.getElementById("cpuTotalChart");
            if (!el) return;
            const labels = (d.cpu_total_series || []).map(it => it.t_label);
            const data = (d.cpu_total_series || []).map(it => it.v);
            if (!CHART) {
                CHART = new Chart(el.getContext("2d"), {
                    type: "line",
                    data: { labels, datasets: [{ label: "CPU %", data, borderColor: "#22d3ee", borderWidth: 1, pointRadius: 0, tension: 0.2, fill: false }] },
                    options: {
                        responsive: true, maintainAspectRatio: false, layout: { padding: 0 }, plugins: { legend: { display: false } },
                        scales: {
                            y: { min: 0, max: 100, ticks: { color: "#94a3b8", stepSize: 25, callback: v => ([0, 25, 50, 75, 100].includes(v) ? v : "") }, grid: { color: "#23283a" } },
                            x: { ticks: { color: "#94a3b8", autoSkip: true, maxTicksLimit: 6 }, grid: { color: "#23283a" } }
                        }
                    }
                });
            } else {
                CHART.data.labels = labels;
                CHART.data.datasets[0].data = data;
                CHART.update("none");
            }
        }

        function renderBars(d, thresholds) {
            const cont = document.getElementById("cpuCoresBars");
            if (!cont) return;
            const cores = d.cores || [];
            cont.classList.toggle("grid2", cores.length > 16);
            const html = cores.map(c => {
                const p = Math.max(0, Math.min(100, Number(c.percent || 0)));
                const color = colorByPct(p, thresholds);
                return `<div class="bar">
        <div class="bar-label">CPU${c.index}</div>
        <div class="bar-track"><div class="bar-fill" style="width:${p}%;background:${color}"></div></div>
        <div class="bar-pct">${p.toFixed(0)}%</div>
      </div>`;
            }).join("");
            cont.innerHTML = html;
        }

        function drawGauge(canvasId, value, range, unit, thresholds, angles) {
            const el = document.getElementById(canvasId);
            if (!el) return;
            const ctx = el.getContext("2d");
            const W = el.width = el.clientWidth;
            const H = el.height = el.clientHeight;

            const startDeg = (angles && Number(angles.start_deg)) || 180;
            const endDeg = (angles && Number(angles.end_deg)) || 360;
            const start = startDeg * Math.PI / 180;
            const endRaw = endDeg * Math.PI / 180;
            const span = ((endRaw - start + Math.PI * 2) % (Math.PI * 2)) || (Math.PI * 2);
            const end = start + span;

            const cx = W / 2;
            const cy = H * 0.45;
            const r = Math.min(W, H * 2) * 0.45;

            const vmin = Number(range.min), vmax = Number(range.max);
            const v = Math.max(vmin, Math.min(vmax, Number(value || 0)));
            const ratio = (v - vmin) / Math.max(1e-6, (vmax - vmin));
            const progress = start + span * ratio;

            ctx.clearRect(0, 0, W, H);
            ctx.lineCap = "round";
            ctx.lineWidth = Math.max(8, r * 0.16);

            ctx.beginPath(); ctx.strokeStyle = "#222533";
            ctx.arc(cx, cy, r, start, end, false); ctx.stroke();

            const pct = ratio * 100.0;
            ctx.beginPath(); ctx.strokeStyle = colorByPct(pct, thresholds);
            ctx.arc(cx, cy, r, start, progress, false); ctx.stroke();

            ctx.fillStyle = "#e2e8f0";
            ctx.font = `bold ${Math.max(14, Math.floor(r * 0.30))}px Consolas`;
            const txt = `${(Number(value || 0)).toFixed(0)}${unit || ""}`;
            const tw = ctx.measureText(txt).width;
            ctx.fillText(txt, cx - tw / 2, cy - r * 0.00);

            ctx.fillStyle = "#94a3b8";
            ctx.font = `${Math.max(10, Math.floor(r * 0.16))}px Consolas`;
            const rangeTxt = `${vmin}${unit || ""} - ${vmax}${unit || ""}`;
            const rw = ctx.measureText(rangeTxt).width;
            ctx.fillText(rangeTxt, cx - rw / 2, cy + r * 0.20);
        }

        function renderGauges(d) {
            const GP = d.gauge_prefs || {};
            const thresholds = GP.thresholds || [50, 80, 95];
            const angles = GP.angles || { start_deg: 180, end_deg: 360 };
            const st = d.stats || {};
            drawGauge("gaugeFreq", Number(st.freq_mhz || 0), GP.freq || { min: 3000, max: 5000 }, "", thresholds, angles);
            drawGauge("gaugeFan", Number(st.fan_rpm || 0), GP.fan || { min: 300, max: 1650 }, "", thresholds, angles);
            drawGauge("gaugeTemp", Number(st.package_temp_c || 0), GP.temp || { min: 25, max: 80 }, "", thresholds, angles);
        }

        function formatRateFromBits(bits, pref) {
            let base = Number(bits || 0), suffix = 'b/s';
            if (pref.type === 'B') { base /= 8; suffix = 'B/s'; }
            const UNIT_ORDER = ['1', 'K', 'M', 'G', 'T'];
            const UNIT_FACTOR = { '1': 1, K: 1e3, M: 1e6, G: 1e9, T: 1e12 };
            const display = (sym) => (sym === '1' ? '' : sym);
            if (pref.auto) {
                const thresholds = [1, 1e3, 1e6, 1e9, 1e12];
                let idx = 0; while (idx < thresholds.length - 1 && base >= thresholds[idx + 1]) idx++;
                const unitSym = UNIT_ORDER[idx], denom = thresholds[idx];
                return { val: base / denom, unit: display(unitSym) + suffix };
            } else {
                const unitSym = (UNIT_ORDER.includes(pref.unit) ? pref.unit : '1');
                return { val: base / (UNIT_FACTOR[unitSym]), unit: display(unitSym) + suffix };
            }
        }

        function renderNetDisk(d) {
            const PREF = d.rate_prefs || { auto: true, unit: 'M', type: 'b' };
            const thresholds = (d.gauge_prefs && d.gauge_prefs.thresholds) || [50, 80, 95];
            const net = d.net || {}, disk = d.disk || {};
            const upF = formatRateFromBits(net.up_bps || 0, PREF);
            const downF = formatRateFromBits(net.down_bps || 0, PREF);
            const readF = formatRateFromBits(disk.read_bps || 0, PREF);
            const writeF = formatRateFromBits(disk.write_bps || 0, PREF);

            const netText = document.getElementById("netText");
            if (netText) netText.innerHTML =
                `<div class="metric-line">上行：<span class="metric-big" style="color:${colorByPct((net.up_bps || 0) / 1e6, thresholds)}">${upF.val.toFixed(2)} ${upF.unit}</span></div>
       <div class="metric-line">下行：<span class="metric-big" style="color:${colorByPct((net.down_bps || 0) / 1e6, thresholds)}">${downF.val.toFixed(2)} ${downF.unit}</span></div>
       <div class="small muted">单位显示：${PREF.type === 'B' ? 'Byte/s' : 'bit/s'}（${PREF.auto ? '自动' : '手动 ' + PREF.unit}）</div>`;

            const diskText = document.getElementById("diskText");
            if (diskText) diskText.innerHTML =
                `<div class="metric-line">读取：<span class="metric-big" style="color:${colorByPct(((disk.read_bps || 0) / 8) / 1e6, thresholds)}">${readF.val.toFixed(2)} ${readF.unit}</span></div>
       <div class="metric-line">写入：<span class="metric-big" style="color:${colorByPct(((disk.write_bps || 0) / 8) / 1e6, thresholds)}">${writeF.val.toFixed(2)} ${writeF.unit}</span></div>
       <div class="small muted">单位显示：${PREF.type === 'B' ? 'Byte/s' : 'bit/s'}（${PREF.auto ? '自动' : '手动 ' + PREF.unit}）</div>`;
        }

        function renderSysAndSw(d) {
            const S = d.sys_info || {};
            renderKV("sysInfoBox", [
                ["操作系统", S.os_version || "N/A"],
                ["CPU 型号", S.cpu_model || "N/A"],
                ["总运行内存", S.ram_total_h || "N/A"],
                ["已使用内存", S.mem_usage_line || "N/A"],
                ["总显存大小", S.vram_total_h || "N/A"],
                ["已使用显存", S.vram_usage_line || "N/A"],
                ["总硬盘容量", S.disk_total_h || "N/A"],
                ["已使用磁盘", S.disk_usage_line || "N/A"],
            ]);
            const V = d.sw_versions || {};
            renderKV("swBox", [
                ["网页服务器版本", V.web_server || V.nginx || "N/A"],
                ["Java 版本", V.java || "N/A"],
                ["Python 版本", V.python || "N/A"],
                ["CUDA 版本", V.cuda || "N/A"],
                ["数据库类型", V.db_type || "N/A"],
                ["数据库版本", V.db_version || "N/A"],
                ["OpenSSH 版本", V.openssh || "N/A"],
            ]);
        }

        function renderKV(boxId, kv) {
            const box = document.getElementById(boxId);
            if (!box) return;
            box.innerHTML = kv.map(([k, v]) => `<div class="muted">${escapeHtml(k)}</div><div>${escapeHtml(v)}</div>`).join("");
        }

        function renderProcs(d) {
            const box = document.getElementById("procBox");
            if (!box) return;
            const procs = d.processes || [];
            const rows = procs.map(p => {
                const inst = Number(p.instances || 0);
                if (inst <= 0) {
                    const details = `<span style="color:#ef4444;font-weight:700;">✖</span> 暂无运行中的进程实例`;
                    return `<div class="muted">${p.name}</div><div>${details}</div>`;
                } else {
                    const cpu = Number(p.cpu_percent || 0).toFixed(1);
                    const memMB = (Number(p.mem_rss || 0) / 1048576).toFixed(1);
                    const details = `实例: ${inst} | CPU: ${cpu}% | 内存: ${memMB} MB`;
                    return `<div class="muted">${p.name}</div><div>${details}</div>`;
                }
            }).join("");
            box.innerHTML = rows;
        }

        function renderTopProcs(d) {
            const box = document.getElementById("topProcBox");
            if (!box) return;
            const list = d.top_procs || [];
            const rows = list.map(p => {
                const cpu = Number(p.cpu_percent || 0).toFixed(1);
                const memMB = (Number(p.mem_rss || 0) / 1048576).toFixed(1);
                const left = `${p.pid} ${(p.name || '')}`;
                const right = `CPU: ${cpu}% | 内存: ${memMB} MB`;
                return `<div class="muted">${left}</div><div>${right}</div>`;
            }).join("");
            box.innerHTML = rows;
        }

        // 自定义区域
        function renderCustom(d) {
            const box = document.getElementById("customBox");
            if (!box) return;
            const cfg = d.custom_area || {};
            const keys = Object.keys(cfg);
            if (keys.length === 1) {
                const k = keys[0];
                const v = cfg[k];

                if (k === "html") {
                    const url = normalizeUrl(v);
                    if (url) {
                        const urlEsc = escapeAttr(url);
                        box.innerHTML =
                            `<iframe class="custom-iframe" src="${urlEsc}"></iframe>` +
                            `<div class="small muted" style="margin-top:6px">若页面被站点策略禁止内嵌，可 <a href="${urlEsc}" target="_blank" rel="noopener">新窗口打开</a></div>`;
                    } else {
                        box.innerHTML = `<div class="muted">提示</div><div>未提供有效的页面地址</div>`;
                    }
                    return;
                }

                if (k === "string") {
                    box.innerHTML = `<div>${escapeHtml(v)}</div>`;
                    return;
                }

                if (k === "img") {
                    const url = normalizeUrl(v);
                    if (url) {
                        box.innerHTML = `<img class="custom-img" src="${escapeAttr(url)}" alt="image">`;
                    } else {
                        box.innerHTML = `<div class="muted">提示</div><div>未提供有效的图片地址</div>`;
                    }
                    return;
                }

                if (k === "link") {
                    const url = normalizeUrl(v);
                    if (url) {
                        box.innerHTML = `<a class="btn-big" href="${escapeAttr(url)}" target="_blank" rel="noopener">打开链接</a>`;
                    } else {
                        box.innerHTML = `<div class="muted">提示</div><div>未提供有效的链接地址</div>`;
                    }
                    return;
                }
            }

            // 兼容旧格式：kv 数组
            if (Array.isArray(cfg.kv) && cfg.kv.length > 0) {
                box.innerHTML = cfg.kv.map(([k, v]) => `<div class="muted">${escapeHtml(k)}</div><div>${escapeHtml(v)}</div>`).join("");
                return;
            }

            // 兜底：将对象其余键值按 KV 显示
            const entries = Object.entries(cfg);
            if (entries.length > 0) {
                box.innerHTML = entries.map(([k, v]) => `<div class="muted">${escapeHtml(k)}</div><div>${escapeHtml(String(v))}</div>`).join("");
                return;
            }

            box.innerHTML = `<div class="muted">提示</div><div>未配置。可设置 {"html":url} | {"string":text} | {"img":url} | {"link":url} 或 {"kv":[["键","值"],...]}</div>`;
        }


        function applyData(d) {
            LATEST = d;
            renderChart(d);
            renderBars(d, (d.gauge_prefs || {}).thresholds || [50, 80, 95]);
            renderGauges(d);
            renderNetDisk(d);
            renderSysAndSw(d);
            renderProcs(d);
            renderTopProcs(d);
            renderCustom(d);
        }

        function loadDataByScript() {
            return new Promise((resolve, reject) => {
                const id = "datajs-loader";
                const old = document.getElementById(id);
                if (old) old.remove();
                const s = document.createElement("script");
                s.id = id;
                s.async = true;
                s.src = "data.js?ts=" + Date.now();
                s.onload = () => {
                    if (window.__DASHBOARD_DATA__) {
                        resolve(window.__DASHBOARD_DATA__);
                    } else {
                        reject(new Error("window.__DASHBOARD_DATA__ missing"));
                    }
                };
                s.onerror = () => reject(new Error("script load failed"));
                document.head.appendChild(s);
            });
        }

        applyData(window.__DASHBOARD_DATA__ || {});

        const POLL_MS_DEFAULT = (LATEST && LATEST.poll_ms) || 5000;
        async function pollOnce() {
            try {
                if (location.protocol === "file:") {
                    const next = await loadDataByScript();
                    applyData(next);
                } else {
                    const resp = await fetch("data.js?ts=" + Date.now(), { cache: "no-store" });
                    const txt = await resp.text();
                    const m = txt.match(/window\.__DASHBOARD_DATA__\s*=\s*(.*);\s*$/s);
                    if (m) {
                        const next = JSON.parse(m[1]);
                        applyData(next);
                    }
                }
                if (CHART) { CHART.resize(); CHART.update("none"); }
            } catch (e) {
                console.error("poll data.js failed:", e);
            } finally {
                const nextMs = (LATEST && LATEST.poll_ms) || POLL_MS_DEFAULT;
                setTimeout(pollOnce, nextMs);
            }
        }
        setTimeout(pollOnce, POLL_MS_DEFAULT);

        window.addEventListener("resize", () => {
            if (LATEST) {
                renderGauges(LATEST);
                if (CHART) { CHART.resize(); CHART.update("none"); }
            }
        });
    })();
