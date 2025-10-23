# Dependencies

本项目使用的第三方组件及其与二进制的精确对应关系如下。

## LibreHardwareMonitor
- Binary: `./LibreHardwareMonitorLib.dll`
- Upstream repo: https://github.com/LibreHardwareMonitor/LibreHardwareMonitor
- Exact source (commit): https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/tree/b807743
- Source provided at (mirror in this repo): `THIRD_PARTY/source-b807743.zip`
- License: MPL-2.0（见 `LICENSES/MPL-2.0.txt`）
- Notices carried from upstream (BSD/LGPL 等)：`THIRD_PARTY/THIRD-PARTY-NOTICES.txt`
- Binary integrity:
  - SHA-256: 见 `THIRD_PARTY/LibreHardwareMonitorLib.SHA256.txt`
  - 验证命令（PowerShell）：
    - `Get-FileHash .\LibreHardwareMonitorLib.dll -Algorithm SHA256`

## 其他运行时依赖（PyPI）
这些为运行时 Python 依赖，通常不随二进制一起分发其源码：
- psutil, PyMySQL, pythonnet, WMI（版本见 `requirements.txt`）
- 其许可证与源码获取方式参见各项目首页或 PyPI。

