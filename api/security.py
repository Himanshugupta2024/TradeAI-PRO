"""Security dependencies for administrative and state-changing endpoints."""
from __future__ import annotations

import hmac
import logging
import os
import re
import secrets
import sys
from fastapi import Header, HTTPException, status
from config import settings

logger = logging.getLogger(__name__)


def require_admin(
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> bool:
    """Validate admin API key from X-Admin-Key, X-API-Key header, or Authorization: Bearer <key>.

    If ADMIN_API_KEY is configured in environment, constant-time compares against incoming key.
    If ADMIN_API_KEY is unset and running in production (RENDER or ENVIRONMENT=production),
    rejects with 503 Service Unavailable for unconfigured admin system.
    In local development with unset ADMIN_API_KEY, permits the request for ease of testing.
    If key is missing: 401 Unauthorized.
    If key is invalid: 403 Forbidden.
    """
    admin_key = (getattr(settings, "ADMIN_API_KEY", "") or os.environ.get("ADMIN_API_KEY", "")).strip()
    is_prod = bool(
        getattr(settings, "IS_PRODUCTION", False)
        or os.getenv("RENDER")
        or os.getenv("ENVIRONMENT") == "production"
    )

    if not admin_key:
        if is_prod:
            logger.error("Admin endpoint accessed but ADMIN_API_KEY is not configured in production")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Admin authentication required. ADMIN_API_KEY is not configured on server.",
            )
        logger.warning("ADMIN_API_KEY not set; allowing admin endpoint in development mode")
        return True

    DEFAULT_DEV_PLACEHOLDERS = {
        "change_this_to_a_strong_random_secret_in_production",
    }
    if admin_key in DEFAULT_DEV_PLACEHOLDERS:
        if is_prod:
            logger.error("Admin endpoint accessed but ADMIN_API_KEY is using insecure default placeholder in production")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Admin authentication misconfigured. Please set a custom ADMIN_API_KEY in production.",
            )
        # In development mode with default placeholder, permit unauthenticated requests for ease of testing
        provided_key = None
        if isinstance(x_admin_key, str) and x_admin_key.strip():
            provided_key = x_admin_key.strip()
        elif isinstance(x_api_key, str) and x_api_key.strip():
            provided_key = x_api_key.strip()
        elif isinstance(authorization, str) and authorization.strip().lower().startswith("bearer "):
            provided_key = authorization.strip()[7:].strip()

        if provided_key is None:
            return True
        if secrets.compare_digest(provided_key, admin_key):
            return True
        op_hash = (getattr(settings, "MARKET_CONTROL_PASSWORD_HASH", "") or os.environ.get("MARKET_CONTROL_PASSWORD_HASH", "")).strip()
        if op_hash:
            import hashlib
            from agents.execution.market_control import _hash_password
            cand_hmac = _hash_password(provided_key)
            cand_sha = hashlib.sha256(provided_key.encode("utf-8")).hexdigest()
            if hmac.compare_digest(cand_hmac, op_hash) or hmac.compare_digest(cand_sha, op_hash):
                return True
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid admin API key",
        )

    provided_key = None
    if isinstance(x_admin_key, str) and x_admin_key.strip():
        provided_key = x_admin_key.strip()
    elif isinstance(x_api_key, str) and x_api_key.strip():
        provided_key = x_api_key.strip()
    elif isinstance(authorization, str) and authorization.strip().lower().startswith("bearer "):
        provided_key = authorization.strip()[7:].strip()

    if not provided_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing admin API key in X-API-Key, X-Admin-Key, or Authorization header",
            headers={"WWW-Authenticate": "ApiKey"},
        )

    if hmac.compare_digest(provided_key, admin_key):
        return True

    op_hash = (getattr(settings, "MARKET_CONTROL_PASSWORD_HASH", "") or os.environ.get("MARKET_CONTROL_PASSWORD_HASH", "")).strip()
    if op_hash:
        import hashlib
        from agents.execution.market_control import _hash_password
        cand_hmac = _hash_password(provided_key)
        cand_sha = hashlib.sha256(provided_key.encode("utf-8")).hexdigest()
        if hmac.compare_digest(cand_hmac, op_hash) or hmac.compare_digest(cand_sha, op_hash):
            return True

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Invalid admin API key",
    )


def get_authenticated_account(
    x_account_id: str | None = Header(default=None, alias="X-Account-ID"),
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> str:
    """Validate account credentials and return the authorized account ID.

    Rules:
    - If no X-Account-ID header is provided:
        - Defaults to 'default' demo account so public dashboard showcase remains functional.
    - If X-Account-ID is provided (e.g. 'acc_alice', 'user_123'):
        - Validates format: 1-64 chars, alphanumeric + dashes/underscores only (prevents injection/traversal).
        - If invalid, raises 400 Bad Request.
        - In production, custom accounts require a valid API key (admin key or credentials).
    - Prevents cross-account data access: requests are isolated strictly to the authorized account_id.
    """
    clean_account = "default"
    if x_account_id is not None:
        raw = x_account_id.strip()
        if not raw or len(raw) > 64 or not re.match(r"^[a-zA-Z0-9_\-]+$", raw):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid X-Account-ID format. Must be 1-64 alphanumeric characters, underscores, or hyphens.",
            )
        clean_account = raw.lower()

    is_prod = bool(
        getattr(settings, "IS_PRODUCTION", False)
        or os.getenv("RENDER")
        or os.getenv("ENVIRONMENT") == "production"
    )
    if clean_account != "default" and is_prod:
        try:
            require_admin(x_admin_key=x_admin_key, x_api_key=x_api_key, authorization=authorization)
        except HTTPException as auth_err:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Authentication required to access isolated account '{clean_account}'.",
                headers={"WWW-Authenticate": "ApiKey"},
            ) from auth_err

    return clean_account
