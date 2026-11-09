"""Publish / apply trained model bundles produced off-box (Colab, Kaggle, CI).

Training on the Render web instance is not an option: the free 512 MB container
cannot hold the ML stack, which is exactly why `LIGHTWEIGHT_API_MODE` exists.
Instead, a notebook trains on free GPU/CPU elsewhere and writes a *bundle* into
the database; the API applies it on explicit admin request.

Security posture: the artifacts are pickles, so applying a bundle is equivalent
to running code. Therefore:
  * only an admin-authenticated call applies anything (never on startup),
  * only whitelisted filenames are written, inside MODELS_DIR,
  * every file is size- and sha256-checked against the manifest,
  * the previous models are backed up first,
  * a metrics gate blocks an obviously broken model unless forced.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from config.logger import get_logger

log = get_logger(__name__)

BUNDLE_KEY = "model_bundle:v1"
MARKER_NAME = ".model_bundle.json"

DEFAULT_MODELS_DIR = Path(__file__).resolve().parent.parent / "models"

# Exactly the artifacts the runtime loads; nothing else is accepted.
ALLOWED_FILES = {
    "xgb_model.pkl",
    "lgbm_model.pkl",
    "logistic_model.pkl",
    "logistic_scaler.pkl",
    "meta_learner.pkl",
    "calibrator.pkl",
    "scaler_params.json",
    "model_meta.json",
    "best_model.pt",
}

MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 12 * 1024 * 1024
MIN_VAL_ACCURACY = float(os.getenv("MODEL_BUNDLE_MIN_ACCURACY", "0.50"))


def models_dir() -> Path:
    return Path(os.getenv("MODELS_DIR") or DEFAULT_MODELS_DIR)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _encode(payload: bytes) -> str:
    return base64.b64encode(gzip.compress(payload, 9)).decode("ascii")


def _decode(encoded: str) -> bytes:
    return gzip.decompress(base64.b64decode(encoded.encode("ascii")))


def build_bundle(
    files: dict[str, bytes],
    *,
    metrics: Optional[dict[str, Any]] = None,
    source: str = "colab",
    git_sha: str = "",
    version: Optional[str] = None,
) -> dict[str, Any]:
    """Assemble a publishable bundle from raw file bytes."""
    unknown = sorted(set(files) - ALLOWED_FILES)
    if unknown:
        raise ValueError(f"Unsupported model artifacts: {unknown}")

    entries = {}
    total = 0
    for name, payload in files.items():
        if len(payload) > MAX_FILE_BYTES:
            raise ValueError(f"{name} is {len(payload)} bytes (limit {MAX_FILE_BYTES})")
        total += len(payload)
        entries[name] = {"sha256": _sha256(payload), "size": len(payload), "data": _encode(payload)}
    if total > MAX_TOTAL_BYTES:
        raise ValueError(f"bundle is {total} bytes (limit {MAX_TOTAL_BYTES})")

    return {
        "version": version or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "git_sha": git_sha,
        "metrics": metrics or {},
        "files": entries,
        "total_bytes": total,
    }


def validate_bundle(bundle: Any, *, force: bool = False) -> tuple[bool, str]:
    """Shape, whitelist, integrity and metrics checks. Returns (ok, reason)."""
    if not isinstance(bundle, dict):
        return False, "bundle is not an object"
    for key in ("version", "files", "metrics"):
        if key not in bundle:
            return False, f"missing '{key}'"
    files = bundle.get("files")
    if not isinstance(files, dict) or not files:
        return False, "no files"

    unknown = sorted(set(files) - ALLOWED_FILES)
    if unknown:
        return False, f"unsupported artifacts: {unknown}"

    total = 0
    for name, entry in files.items():
        if not isinstance(entry, dict):
            return False, f"{name}: malformed entry"
        size = int(entry.get("size") or 0)
        if size <= 0 or size > MAX_FILE_BYTES:
            return False, f"{name}: size {size} outside limits"
        total += size
        if not entry.get("sha256") or not entry.get("data"):
            return False, f"{name}: missing sha256/data"
    if total > MAX_TOTAL_BYTES:
        return False, f"bundle too large: {total}"

    if not force:
        accuracy = (bundle.get("metrics") or {}).get("val_accuracy")
        if accuracy is not None:
            try:
                if float(accuracy) < MIN_VAL_ACCURACY:
                    return False, f"val_accuracy {accuracy} below gate {MIN_VAL_ACCURACY}"
            except (TypeError, ValueError):
                return False, "val_accuracy is not numeric"
    return True, "ok"


def apply_bundle(bundle: dict[str, Any], *, target_dir: Optional[Path] = None) -> dict[str, Any]:
    """Verify every file, back up the current models, then write atomically."""
    ok, reason = validate_bundle(bundle, force=True)
    if not ok:
        return {"ok": False, "error": reason}

    out_dir = Path(target_dir or models_dir())
    out_dir.mkdir(parents=True, exist_ok=True)

    decoded: dict[str, bytes] = {}
    for name, entry in bundle["files"].items():
        payload = _decode(entry["data"])
        if len(payload) != int(entry["size"]) or _sha256(payload) != entry["sha256"]:
            return {"ok": False, "error": f"{name}: integrity check failed"}
        decoded[name] = payload

    backup_dir = out_dir / f"backup-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    existing = [n for n in decoded if (out_dir / n).exists()]
    if existing:
        backup_dir.mkdir(parents=True, exist_ok=True)
        for name in existing:
            try:
                shutil.copy2(out_dir / name, backup_dir / name)
            except Exception as exc:  # pragma: no cover - fs edge case
                log.warning("[model-sync] backup of %s failed: %s", name, exc)

    written = []
    for name, payload in decoded.items():
        tmp = out_dir / f".{name}.tmp"
        tmp.write_bytes(payload)
        os.replace(tmp, out_dir / name)
        written.append(name)

    marker = {
        "version": bundle.get("version"),
        "trained_at": bundle.get("trained_at"),
        "source": bundle.get("source"),
        "git_sha": bundle.get("git_sha"),
        "metrics": bundle.get("metrics") or {},
        "files": sorted(written),
        "applied_at": datetime.now(timezone.utc).isoformat(),
        "backup_dir": str(backup_dir) if existing else None,
    }
    (out_dir / MARKER_NAME).write_text(json.dumps(marker, indent=2), encoding="utf-8")
    log.info("[model-sync] applied bundle %s (%d files)", marker["version"], len(written))
    return {"ok": True, **marker}


def current_status(target_dir: Optional[Path] = None) -> dict[str, Any]:
    """What is on disk right now (no artifact content)."""
    out_dir = Path(target_dir or models_dir())
    marker_path = out_dir / MARKER_NAME
    marker: dict[str, Any] = {}
    if marker_path.exists():
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
        except Exception:
            marker = {}
    present = sorted(p.name for p in out_dir.glob("*") if p.is_file() and not p.name.startswith("."))
    return {
        "models_dir": str(out_dir),
        "bundle": marker or None,
        "files_present": present,
        "min_accuracy_gate": MIN_VAL_ACCURACY,
    }


def read_bundle_from_db() -> Optional[dict[str, Any]]:
    from db.runtime_state import get_runtime_state

    bundle = get_runtime_state(BUNDLE_KEY)
    return bundle if isinstance(bundle, dict) else None


def sync_from_db(*, target_dir: Optional[Path] = None, force: bool = False) -> dict[str, Any]:
    """Apply the newest published bundle, if it passes the gates."""
    try:
        bundle = read_bundle_from_db()
    except Exception as exc:
        return {"ok": False, "error": f"could not read bundle: {exc}"}
    if not bundle:
        return {"ok": False, "error": "no bundle published", "applied": False}

    ok, reason = validate_bundle(bundle, force=force)
    if not ok:
        return {"ok": False, "error": reason, "applied": False}

    status = current_status(target_dir)
    current_version = (status.get("bundle") or {}).get("version")
    if current_version and str(bundle.get("version")) <= str(current_version) and not force:
        return {
            "ok": True,
            "applied": False,
            "reason": f"already at {current_version}",
            "version": current_version,
        }

    result = apply_bundle(bundle, target_dir=target_dir)
    result["applied"] = bool(result.get("ok"))
    return result
