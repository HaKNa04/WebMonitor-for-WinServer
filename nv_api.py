import ctypes
import logging
import platform
from typing import Optional, Tuple, List

# =========================
# NVML（NVIDIA 驱动 API）轻量封装
# =========================
class _nvmlMemory_t(ctypes.Structure):
    _fields_ = [
        ("total", ctypes.c_uint64),
        ("free", ctypes.c_uint64),
        ("used", ctypes.c_uint64),
    ]

class NVMLHelper:
    def __init__(self, debug: bool = False) -> None:
        self.debug = debug
        self._lib = None
        self._initialized = False
        self._load_lib()

    def _load_lib(self):
        if platform.system().lower() == "windows":
            try:
                self._lib = ctypes.WinDLL("nvml", use_last_error=True)
            except Exception:
                self._lib = None
        else:
            try:
                self._lib = ctypes.CDLL("libnvidia-ml.so.1")
            except Exception:
                self._lib = None
        if not self._lib:
            return
        # 绑定函数（尽量兼容 *_v2 与旧符号）
        def _bind(name_alt: List[str], restype, argtypes=None):
            for n in name_alt:
                try:
                    fn = getattr(self._lib, n)
                    fn.restype = restype
                    if argtypes is not None:
                        fn.argtypes = argtypes
                    return fn
                except Exception:
                    continue
            return None

        self.nvmlInit = _bind(["nvmlInit_v2", "nvmlInit"], ctypes.c_int, [])
        self.nvmlShutdown = _bind(["nvmlShutdown"], ctypes.c_int, [])
        self.nvmlDeviceGetCount = _bind(["nvmlDeviceGetCount_v2", "nvmlDeviceGetCount"], ctypes.c_int, [ctypes.POINTER(ctypes.c_uint)])
        self.nvmlDeviceGetHandleByIndex = _bind(["nvmlDeviceGetHandleByIndex_v2", "nvmlDeviceGetHandleByIndex"], ctypes.c_int, [ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p)])
        self.nvmlDeviceGetMemoryInfo = _bind(["nvmlDeviceGetMemoryInfo"], ctypes.c_int, [ctypes.c_void_p, ctypes.POINTER(_nvmlMemory_t)])
        self.nvmlSystemGetCudaDriverVersion = _bind(["nvmlSystemGetCudaDriverVersion_v2", "nvmlSystemGetCudaDriverVersion"], ctypes.c_int, [ctypes.POINTER(ctypes.c_int)])
        self.nvmlErrorString = _bind(["nvmlErrorString"], ctypes.c_char_p, [ctypes.c_int])

    def available(self) -> bool:
        return self._lib is not None and self.nvmlInit is not None

    def _check(self, rc: int, ctx: str) -> bool:
        if rc == 0:
            return True
        if self.debug:
            try:
                err = self.nvmlErrorString(rc) if self.nvmlErrorString else None
                logging.debug(f"NVML {ctx} failed rc={rc} msg={(err.decode('utf-8') if err else 'N/A')}")
            except Exception:
                logging.debug(f"NVML {ctx} failed rc={rc}")
        return False

    def init(self) -> bool:
        if not self.available():
            return False
        if self._initialized:
            return True
        rc = self.nvmlInit()
        ok = self._check(rc, "Init")
        self._initialized = ok
        return ok

    def shutdown(self) -> None:
        if not self._initialized:
            return
        try:
            rc = self.nvmlShutdown()
            self._check(rc, "Shutdown")
        finally:
            self._initialized = False

    def cuda_driver_version_str(self) -> Optional[str]:
        if not self.init():
            return None
        if not self.nvmlSystemGetCudaDriverVersion:
            return None
        v = ctypes.c_int(0)
        rc = self.nvmlSystemGetCudaDriverVersion(ctypes.byref(v))
        if not self._check(rc, "SystemGetCudaDriverVersion"):
            return None
        # v 如 12080 -> 12.8
        ival = int(v.value)
        major = ival // 1000
        minor = (ival % 1000) // 10
        return f"{major}.{minor}"

    def gpu_mem_sum(self) -> Tuple[Optional[int], Optional[int]]:
        """
        返回 (used_bytes_sum, total_bytes_sum)
        """
        if not self.init():
            return None, None
        if not (self.nvmlDeviceGetCount and self.nvmlDeviceGetHandleByIndex and self.nvmlDeviceGetMemoryInfo):
            return None, None
        cnt = ctypes.c_uint(0)
        rc = self.nvmlDeviceGetCount(ctypes.byref(cnt))
        if not self._check(rc, "DeviceGetCount"):
            return None, None
        total = 0
        used = 0
        for i in range(int(cnt.value)):
            h = ctypes.c_void_p()
            rc = self.nvmlDeviceGetHandleByIndex(ctypes.c_uint(i), ctypes.byref(h))
            if not self._check(rc, f"DeviceGetHandleByIndex({i})"):
                continue
            mem = _nvmlMemory_t()
            rc = self.nvmlDeviceGetMemoryInfo(h, ctypes.byref(mem))
            if not self._check(rc, f"DeviceGetMemoryInfo({i})"):
                continue
            total += int(mem.total)
            used += int(mem.used)
        if total == 0:
            return None, None
        return used, total

