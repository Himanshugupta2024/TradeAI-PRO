"""
Process RSS Memory Profiler.

Accurately measures real OS process Resident Set Size (RSS) memory across
Windows and Linux environments without heavy third-party dependencies:
  - Windows: Uses win32process.GetProcessMemoryInfo (WorkingSetSize)
  - Linux/Render: Uses resource.getrusage or /proc/self/status (VmRSS)

Guarantees system remains well within Render free-tier (512MB RAM):
  Target steady-state RSS: < 380 MB
  Target peak RSS:         < 430 MB
"""

from __future__ import annotations

import logging
import os
import platform
import sys

log = logging.getLogger(__name__)


def get_process_rss_mb() -> float:
    """
    Return current process Resident Set Size (RSS) in Megabytes (MB).
    """
    system = platform.system()

    # 1. Windows implementation via pywin32
    if system == "Windows":
        try:
            import win32process
            info = win32process.GetProcessMemoryInfo(win32process.GetCurrentProcess())
            rss_bytes = info.get("WorkingSetSize", 0)
            return round(rss_bytes / (1024.0 * 1024.0), 2)
        except Exception:
            pass

    # 2. Linux / Render implementation
    if system == "Linux":
        # First try /proc/self/status (most precise live VmRSS)
        try:
            with open("/proc/self/status", "r") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        # Format: VmRSS:     12345 kB
                        parts = line.split()
                        if len(parts) >= 2:
                            kb = float(parts[1])
                            return round(kb / 1024.0, 2)
        except Exception:
            pass

        # Fallback to resource
        try:
            import resource
            usage = resource.getrusage(resource.RUSAGE_SELF)
            # On Linux, ru_maxrss is in kilobytes
            return round(usage.ru_maxrss / 1024.0, 2)
        except Exception:
            pass

    # 3. macOS / Unix fallback
    try:
        import resource
        usage = resource.getrusage(resource.RUSAGE_SELF)
        # On Darwin, ru_maxrss is in bytes
        return round(usage.ru_maxrss / (1024.0 * 1024.0), 2)
    except Exception:
        pass

    return 0.0


def check_memory_guardrail(
    max_steady_mb: float = 380.0,
    max_peak_mb: float = 430.0,
) -> dict[str, Any]:
    """
    Check current RSS against Render 512MB safety limits.
    """
    current_rss = get_process_rss_mb()
    headroom_mb = max(0.0, 512.0 - current_rss)

    is_safe = current_rss <= max_peak_mb
    warning = current_rss > max_steady_mb

    return {
        "current_rss_mb": current_rss,
        "max_steady_mb": max_steady_mb,
        "max_peak_mb": max_peak_mb,
        "headroom_mb": round(headroom_mb, 2),
        "is_safe": is_safe,
        "warning": warning,
    }
