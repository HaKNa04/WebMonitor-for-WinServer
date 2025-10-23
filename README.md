# WebMonitor for Windows Server（服务器监控仪表盘）

一个基于 Python 的轻量级服务器监控与展示方案。后端以 psutil/NVML/LibreHardwareMonitor（通过 pythonnet 和 `LibreHardwareMonitorLib.dll`）采集系统与硬件指标，数据以静态 `data.js` 输出；前端使用 Chart.js 与原生 JS 模板实时渲染，无需运行后端 Web 服务即可展示。

- 后端：`WebMmonitor.py` 定时采集并输出数据（可选写入 DB）
- 前端：`web/index.template.html` + `web/dashboard.template.js` + `chart.4.4.4.min.js`
- 存储：可选 DB 持久化（自动建库建表），或纯文件模式（仅生成静态页面与 `data.js`）

---

## 运行环境与依赖

- 操作系统：Windows 10/11/Server（需要管理员权限以访问硬件/驱动）
- 显卡驱动：如需 NVML（NVIDIA）支持，请安装 NVIDIA 驱动
- LibreHardwareMonitor：已经集成 `LibreHardwareMonitorLib.dll` ，来自[LibreHardwareMonitor v0.9.4](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor)
- Python：编译测试环境为3.12（建议 3.9/3.10/3.11）
  - psutil==5.9.8
  - PyMySQL==1.1.1
  - pythonnet==3.0.5
  - WMI==1.5.1

---

## 功能概览

- CPU
  - 总体占用曲线（近 10 分钟）
  - 各逻辑核心占用条形图
  - 频率、风扇转速、核心温度仪表盘
- 网络/磁盘
  - 上下行速率、读写速率
- 系统/软件信息
  - OS、CPU 型号、内存/显存/磁盘总量与使用情况
  - Web 服务器、Java、Python、CUDA、数据库版本
- 进程监控
  - 自定义进程项“显示名=路径子串”匹配、实例数、CPU%、内存
  - 全局进程 CPU 咋弄排行
- 自定义窗口
  -目前支持
    - img: 输入图片URL。示例：{"img": "https://http.cat/200.jpg"}
    - html: 进行内嵌网页。可以指定一个 URL 或 HTML 文件。
    - link: 超链接。示例：{"link": "www.google.com"}
    - kv: (这个能用吗？可能没做好？呜呜呜...)
- 输出与前端
  - 将数据写入静态 `data.js`，前端以轮询方式自动刷新
  - 模板自动写入/覆盖（HTML/JS/Chart.js 可本地化）
  - 自定义区域支持内嵌网页、图片、链接或 KV 列表

---
 
## 常见问题
- FileNotFoundError: [WinError 3] 系统找不到指定的路径。: ''
  - 没有在 app_config.py 中配置文件输出目录。

- 无法读取风扇/温度，或者无法运行
  - 以管理员权限运行；由于 Windows UAC 和 SuperIO 的安全问题，获取风扇转速等信息必须使用管理员身份。

- 一启动就输出了很多数据，有很多 LHM | SuperIO 之类的内容
  - 因为你还没有选择测温点，设备上通常会有很多的测温点，请输入序号和回车进行选择
  - 下一步同样会出现风扇测速点，请按照同样的方式进行选择
  - 只有初次使用需要选择，后续会保存到配置文件中

- 进程匹配过多导致终止
  - 不要检测 "C://" 之类的路径，请指定一个详细的路径，过于宽泛的目录或公共名或导致查询时间变长

- 显存总量显示 N/A
  - 需要 LHM 或 NVML 或 WMI 的支持（不同系统/驱动可能不可用）
  - 目前只兼容了 NVIDIA GPU ，后续可能会修改代码以适配 AMD GPU 和 Intel GPU ?

---

## License
- Project License: Apache-2.0（见 `LICENSE`）
- Third-party Licenses:
  - MPL-2.0 正文：`LICENSES/MPL-2.0.txt`
  - 上游随附第三方告知：`THIRD_PARTY/THIRD-PARTY-NOTICES.txt`

## Third-party
- LibreHardwareMonitorLib.dll（MPL-2.0）
  - Binary: `./LibreHardwareMonitorLib.dll`
  - Upstream: https://github.com/LibreHardwareMonitor/LibreHardwareMonitor
  - Exact commit: `b807743`
  - Source provided at: `THIRD_PARTY/source-b807743.zip`
  - Hash (SHA-256): 见 `THIRD_PARTY/LibreHardwareMonitorLib.SHA256.txt`
  - License text: `LICENSES/MPL-2.0.txt`
  - Additional notices from upstream: `THIRD_PARTY/THIRD-PARTY-NOTICES.txt`