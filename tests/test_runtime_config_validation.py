from __future__ import annotations

import pytest

from config import settings


def test_validate_runtime_config_passes_with_defaults() -> None:
    settings.validate_runtime_config()


def test_validate_runtime_config_rejects_bad_mtf_weights(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "MTF_WEIGHTS", {"1d": 0.6, "1h": 0.6}, raising=False)
    with pytest.raises(ValueError, match="MTF_WEIGHTS"):
        settings.validate_runtime_config()


def test_validate_runtime_config_rejects_negative_intraday_age(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "INTRADAY_MAX_BAR_AGE_MINUTES", -1, raising=False)
    with pytest.raises(ValueError, match="INTRADAY_MAX_BAR_AGE_MINUTES"):
        settings.validate_runtime_config()


def test_validate_runtime_config_rejects_invalid_refresh_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "REFRESH_CYCLE_TIMEOUT_SEC", 20, raising=False)
    with pytest.raises(ValueError, match="REFRESH_CYCLE_TIMEOUT_SEC"):
        settings.validate_runtime_config()
