import pytest
from fastapi import HTTPException
from config import settings
from api.security import require_admin


def test_require_admin_dev_mode_allowed(monkeypatch):
    """In development when ADMIN_API_KEY is unset, allow with warning."""
    monkeypatch.delenv("ADMIN_API_KEY", raising=False)
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.setattr(settings, "ADMIN_API_KEY", "")

    assert require_admin() is True


def test_require_admin_production_unset_rejected(monkeypatch):
    """In production when ADMIN_API_KEY is unset, fail-closed with 503."""
    monkeypatch.delenv("ADMIN_API_KEY", raising=False)
    monkeypatch.setattr(settings, "ADMIN_API_KEY", "")
    monkeypatch.setenv("RENDER", "true")

    with pytest.raises(HTTPException) as exc_info:
        require_admin()
    assert exc_info.value.status_code == 503
    assert "ADMIN_API_KEY is not configured" in exc_info.value.detail


def test_require_admin_configured_keys(monkeypatch):
    """When ADMIN_API_KEY is set, authenticate via X-Admin-Key, X-API-Key, or Bearer header."""
    monkeypatch.setenv("ADMIN_API_KEY", "secret-test-token-123")
    monkeypatch.setattr(settings, "ADMIN_API_KEY", "secret-test-token-123")

    # Correct X-Admin-Key header
    assert require_admin(x_admin_key="secret-test-token-123") is True
    # Correct X-API-Key header
    assert require_admin(x_api_key="secret-test-token-123") is True
    # Correct Bearer token
    assert require_admin(authorization="Bearer secret-test-token-123") is True

    # Missing header -> 401 Unauthorized
    with pytest.raises(HTTPException) as exc_missing:
        require_admin()
    assert exc_missing.value.status_code == 401

    # Wrong header -> 403 Forbidden
    with pytest.raises(HTTPException) as exc_wrong:
        require_admin(x_admin_key="wrong-token")
    assert exc_wrong.value.status_code == 403


def test_require_admin_default_placeholder_dev_vs_prod(monkeypatch):
    """Default template placeholder allows in dev, but rejects with 503 in prod."""
    placeholder = "change_this_to_a_strong_random_secret_in_production"
    monkeypatch.setenv("ADMIN_API_KEY", placeholder)
    monkeypatch.setattr(settings, "ADMIN_API_KEY", placeholder)

    # In Dev
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    assert require_admin() is True

    # In Prod
    monkeypatch.setenv("RENDER", "true")
    with pytest.raises(HTTPException) as exc_prod:
        require_admin()
    assert exc_prod.value.status_code == 503
    assert "set a custom ADMIN_API_KEY" in exc_prod.value.detail
