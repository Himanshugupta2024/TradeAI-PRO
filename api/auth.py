"""
Authentication and authorization utilities for TradeAI-PRO API routes.
Enforces admin API key authentication on state-changing and sensitive endpoints.
"""

from __future__ import annotations

import logging
import secrets
from fastapi import HTTPException, Security, status
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer

from config import settings

logger = logging.getLogger(__name__)

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
_bearer_security = HTTPBearer(auto_error=False)


def verify_admin_api_key(
    key_from_header: str | None = Security(_api_key_header),
    bearer_creds: HTTPAuthorizationCredentials | None = Security(_bearer_security),
) -> str:
    """
    Enforce admin API key authentication for protected endpoints.

    Accepts key via:
      - X-API-Key header
      - Authorization: Bearer <key> header

    Security rules:
      - In production (IS_PRODUCTION=True): ADMIN_API_KEY must be set. If not configured, returns 503.
      - If ADMIN_API_KEY is configured:
          - Missing key returns 401 Unauthorized.
          - Incorrect key returns 403 Forbidden using constant-time comparison.
      - In development/testing (IS_PRODUCTION=False):
          - If ADMIN_API_KEY is unset/empty, allows dev bypass.
          - If ADMIN_API_KEY is set, enforces authentication strictly.
    """
    configured_key = getattr(settings, "ADMIN_API_KEY", "") or ""
    is_prod = getattr(settings, "IS_PRODUCTION", False)

    # In production, missing admin key configuration is a server misconfiguration
    if is_prod and not configured_key:
        logger.error("Admin action attempted in production, but ADMIN_API_KEY is not configured.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Admin authentication is not configured on this server.",
        )

    # In dev/testing, if ADMIN_API_KEY is not set, allow requests
    if not configured_key:
        return "dev-bypass"

    # Extract provided key from either header
    provided_key = key_from_header
    if not provided_key and bearer_creds and bearer_creds.credentials:
        provided_key = bearer_creds.credentials

    if not provided_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing admin API key in X-API-Key or Authorization Bearer header.",
            headers={"WWW-Authenticate": "ApiKey"},
        )

    # Constant-time comparison to prevent timing attacks
    if not secrets.compare_digest(provided_key.encode("utf-8"), configured_key.encode("utf-8")):
        logger.warning("Rejected invalid admin API key attempt.")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid admin API key.",
        )

    return provided_key


# Alias for compatibility with security dependency references
require_admin = verify_admin_api_key

