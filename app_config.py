import os
# =========================
# 配置（未指定命令行参数时使用）
# =========================

# 禁用数据库（仅显示，不入库）
DEFAULT_DISABLE_DB = True

# 调试开关
DEFAULT_DEBUG = False

# 数据库连接参数
DEFAULT_DB_HOST = "127.0.0.1"
DEFAULT_DB_PORT = 3306
DEFAULT_DB_USER = "username?"
DEFAULT_DB_PASS = "password?"
DEFAULT_DB_NAME = "server_monitor_app"

# 文件输出目录及文件名
# example: r"L:\nginx-1.28.0\html\server_info", So,you can access it via "http://localhost/server_info"
DEFAULT_OUT_DIR = r""
DEFAULT_HTML_NAME = "index.html"
DEFAULT_JS_NAME = "dashboard.js"
DEFAULT_DATA_JS_NAME = "data.js"

# Web 服务器路径（可执行文件或目录），用于辅助识别；留空表示自动从 PATH/系统检测（本版不再执行外部命令，仅用于文件版本读取）
# example: r"L:\nginx-1.28.0",其中包含 nginx.exe
DEFAULT_WEB_SERVER_PATH = r""

# 需要执行搜索的进程名字符串，分号或逗号分隔，使用“显示名=路径子串”（大小写不敏感，按 exe 完整路径连续子串匹配）
# example: "Nginx=nginx.exe;Java=\\Java\\;Python=python.exe;DDNS=ddns-go.exe;MariaDB=mysqld.exe;MCSM=mcsm;NapCat=napcat;OpenSSH=sshd.exe;Frpc=frpc.exe"
DEFAULT_PROCESS_NAMES = "test1=test1;Nginx=nginx.exe;Java=\\Java\\;MariaDB=mysqld.exe"

# 采样周期（秒）
DEFAULT_INTERVAL = 5

# 启动清空历史 & 数据保留分钟数（<=0 禁用自动清理）
DEFAULT_WIPE_ON_START = True
DEFAULT_RETENTION_MINUTES = 60

# 速率显示偏好：
DEFAULT_RATE_AUTO_SCALE = True  # 自动缩放单位
DEFAULT_RATE_MANUAL_UNIT = "M"  # '1'|'K'|'M'|'G'|'T'
DEFAULT_RATE_UNIT_TYPE = "B"    # 'b'|'B' (bit ot Byte)

# 是否强制覆盖生成的 HTML/JS 资源文件
DEFAULT_OVERWRITE_ASSETS = True

# —— 仪表盘参数（可按需修改）——

# CPU_FREQUENCY RANGE (MHz)
DEFAULT_GAUGE_FREQ_MIN = 1000
DEFAULT_GAUGE_FREQ_MAX = 4630

# CPU_FAN_SPEED RANGE (RPM)
DEFAULT_GAUGE_FAN_MIN  = 300
DEFAULT_GAUGE_FAN_MAX  = 1650

# CPU_TEMPERATURE RANGE (°C)
DEFAULT_GAUGE_TEMP_MIN = 25
DEFAULT_GAUGE_TEMP_MAX = 80
DEFAULT_GAUGE_THRESHOLDS = (50, 80, 95)
DEFAULT_GAUGE_ANGLE_START_DEG = 135
DEFAULT_GAUGE_ANGLE_END_DEG   = 45

# LibreHardwareMonitor config
DEFAULT_LHM_CONFIG = "LibreHM.config"

# html/js template path
DEFAULT_HTML_TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "web", "index.template.html")
DEFAULT_JS_TEMPLATE_PATH   = os.path.join(os.path.dirname(__file__), "web", "dashboard.template.js")

# 本地 Chart.js 路径（建议放置于 web 目录）
DEFAULT_CHART_JS_PATH = os.path.join(os.path.dirname(__file__), "web", "chart.4.4.4.min.js")

# 自定义信息区
DEFAULT_CUSTOM_AREA = {"img": "https://http.cat/200.jpg"}