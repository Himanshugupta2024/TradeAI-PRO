"""Atomic file I/O helpers for JSON persistence."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def atomic_write_json(
    path: str | Path,
    data: Any,
    *,
    indent: int | None = None,
    ensure_ascii: bool = True,
    default: Any = None,
    encoding: str = "utf-8",
) -> None:
    """Write JSON atomically using temp-file + fsync + os.replace."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            json.dump(data, f, indent=indent, ensure_ascii=ensure_ascii, default=default)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, target)
    except Exception:
        try:
            os.remove(tmp_name)
        except OSError:
            pass
        raise
