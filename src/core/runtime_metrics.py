from __future__ import annotations

import os
from typing import Any


def release_process_working_set() -> bool:
    """Best-effort release of freed pages between documents on Windows."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.SetProcessWorkingSetSize.argtypes = [wintypes.HANDLE, ctypes.c_size_t, ctypes.c_size_t]
        kernel32.SetProcessWorkingSetSize.restype = wintypes.BOOL
        process = kernel32.GetCurrentProcess()
        maximum = ctypes.c_size_t(-1).value
        return bool(kernel32.SetProcessWorkingSetSize(process, maximum, maximum))
    except (AttributeError, OSError, TypeError, ValueError):
        return False


def process_memory_mb() -> dict[str, float] | None:
    """Return current and peak working-set sizes without an optional dependency."""
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            class ProcessMemoryCounters(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            psapi = ctypes.WinDLL("psapi", use_last_error=True)
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            psapi.GetProcessMemoryInfo.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(ProcessMemoryCounters),
                wintypes.DWORD,
            ]
            psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
            counters = ProcessMemoryCounters()
            counters.cb = ctypes.sizeof(counters)
            process = kernel32.GetCurrentProcess()
            ok = psapi.GetProcessMemoryInfo(process, ctypes.byref(counters), counters.cb)
            if not ok:
                return None
            divisor = 1024 * 1024
            return {
                "rss_mb": round(counters.WorkingSetSize / divisor, 1),
                "peak_rss_mb": round(counters.PeakWorkingSetSize / divisor, 1),
            }
        except (AttributeError, OSError, TypeError, ValueError):
            return None

    try:
        import resource

        usage: Any = resource.getrusage(resource.RUSAGE_SELF)
        divisor = 1024 if usage.ru_maxrss > 1024 * 1024 else 1
        peak = usage.ru_maxrss / divisor
        return {"rss_mb": round(peak, 1), "peak_rss_mb": round(peak, 1)}
    except (ImportError, AttributeError, OSError, TypeError, ValueError):
        return None


def format_memory(label: str) -> str:
    values = process_memory_mb()
    if values is None:
        return f"  [memory] {label} unavailable"
    return f"  [memory] {label} rss={values['rss_mb']:.1f}MB peak={values['peak_rss_mb']:.1f}MB"
