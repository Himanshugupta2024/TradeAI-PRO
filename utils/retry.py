"""
Retry and timeout utilities for external API calls.

Provides:
  - async_retry: async function retry with exponential backoff
  - retry_with_timeout: combine retry + timeout
  - structured error responses
"""

import asyncio
import logging
from functools import wraps
from typing import TypeVar, Callable, Any, Optional

logger = logging.getLogger(__name__)

T = TypeVar("T")


async def async_retry(
    func: Callable[..., Any],
    *args,
    max_retries: int = 3,
    backoff_factor: float = 2.0,
    initial_delay: float = 1.0,
    **kwargs,
) -> Any:
    """
    Retry an async function with exponential backoff.
    
    Args:
        func: Async function to call
        max_retries: Maximum number of retry attempts
        backoff_factor: Multiplier for delay between retries
        initial_delay: First retry delay in seconds
        *args, **kwargs: Arguments to pass to func
        
    Returns:
        Function result or raises last exception after max_retries
    """
    last_exc = None
    delay = initial_delay
    
    for attempt in range(max_retries):
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            last_exc = e
            if attempt < max_retries - 1:
                logger.debug(
                    "Retry %d/%d after %.1fs — %s: %s",
                    attempt + 1, max_retries, delay, type(e).__name__, str(e)[:80]
                )
                await asyncio.sleep(delay)
                delay *= backoff_factor
            else:
                logger.warning(
                    "Max retries (%d) exhausted for %s: %s",
                    max_retries, getattr(func, "__name__", "callable"), str(e)[:100]
                )
    
    raise last_exc


async def async_retry_with_timeout(
    func: Callable[..., Any],
    timeout: float = 30.0,
    max_retries: int = 2,
    backoff_factor: float = 2.0,
    *args,
    **kwargs,
) -> Any:
    """
    Retry an async function with timeout on each attempt.
    
    Args:
        func: Async function to call
        timeout: Timeout per attempt (seconds)
        max_retries: Maximum retry attempts
        backoff_factor: Delay multiplier between retries
        *args, **kwargs: Arguments to pass to func
        
    Returns:
        Function result
        
    Raises:
        asyncio.TimeoutError: If timeout exceeded on all retries
        Last exception from function
    """
    delay = 1.0
    last_exc = None
    
    for attempt in range(max_retries):
        try:
            return await asyncio.wait_for(func(*args, **kwargs), timeout=timeout)
        except asyncio.TimeoutError as e:
            last_exc = e
            if attempt < max_retries - 1:
                logger.debug(
                    "Timeout on attempt %d/%d, retrying after %.1fs...",
                    attempt + 1, max_retries, delay
                )
                await asyncio.sleep(delay)
                delay *= backoff_factor
            else:
                logger.warning(
                    "Timeout on all %d retries (timeout=%.1fs) for %s",
                    max_retries, timeout, getattr(func, "__name__", "callable")
                )
        except Exception as e:
            last_exc = e
            if attempt < max_retries - 1:
                logger.debug(
                    "Error on attempt %d/%d: %s, retrying...",
                    attempt + 1, max_retries, str(e)[:80]
                )
                await asyncio.sleep(delay)
                delay *= backoff_factor
            else:
                logger.warning(
                    "Max retries exhausted: %s", str(e)[:100]
                )
    
    raise last_exc


def safe_json_response(status: str, message: str = "", data: Optional[dict] = None) -> dict:
    """
    Create a safe JSON response structure for API endpoints.
    
    Args:
        status: "ok" or "error"
        message: Error/info message
        data: Optional data payload
        
    Returns:
        Dict ready for JSONResponse
    """
    resp = {"status": status, "message": message}
    if data is not None:
        resp["data"] = data
    return resp
