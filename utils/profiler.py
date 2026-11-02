"""
Performance Profiling and Latency Telemetry for TradeAI-PRO.

Provides zero-overhead high-precision instrumentation using time.perf_counter().
Emits structured timing logs when execution duration exceeds configurable threshold.
"""
from __future__ import annotations

import functools
import logging
import os
import time
from contextlib import contextmanager
from typing import Any, Callable

logger = logging.getLogger("perf")

SLOW_OPERATION_THRESHOLD_MS = float(os.getenv("SLOW_OPERATION_THRESHOLD_MS", "150.0"))


class PerfTracker:
    """Collects fine-grained operation timings and formats structured performance metrics."""

    def __init__(self, operation_name: str, slow_threshold_ms: float = SLOW_OPERATION_THRESHOLD_MS):
        self.operation_name = operation_name
        self.slow_threshold_ms = slow_threshold_ms
        self.timings: dict[str, float] = {}
        self.start_time = time.perf_counter()
        self.metadata: dict[str, Any] = {}

    def record(self, step_name: str, duration_ms: float) -> None:
        """Record duration of a named sub-step in milliseconds."""
        self.timings[step_name] = round(duration_ms, 2)

    def set_meta(self, key: str, value: Any) -> None:
        """Set contextual metadata (e.g. cache_hit=True, symbols=50)."""
        self.metadata[key] = value

    def finish(self) -> float:
        """Complete timing, log if slow or debug enabled, and return total duration in ms."""
        total_ms = (time.perf_counter() - self.start_time) * 1000.0
        self.timings["total"] = round(total_ms, 2)

        is_slow = total_ms >= self.slow_threshold_ms
        if is_slow or logger.isEnabledFor(logging.DEBUG):
            # Format: PERF signals/all total=42.8ms cache_lookup=1.2ms ml=17.2ms cache_hit=true
            step_str = " ".join(f"{k}={v}ms" for k, v in self.timings.items() if k != "total")
            meta_str = " ".join(f"{k}={v}" for k, v in self.metadata.items())
            log_line = f"PERF {self.operation_name} total={self.timings['total']}ms {step_str} {meta_str}".strip()

            if is_slow:
                logger.info(log_line)
            else:
                logger.debug(log_line)

        return total_ms


@contextmanager
def measure_perf(step_name: str, tracker: PerfTracker | None = None):
    """Context manager to measure execution time of a code block."""
    t0 = time.perf_counter()
    try:
        yield
    finally:
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        if tracker is not None:
            tracker.record(step_name, elapsed_ms)
