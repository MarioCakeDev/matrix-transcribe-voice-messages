import asyncio
import logging
import random
from typing import Any, Callable, Mapping

import aiohttp

logger = logging.getLogger(__name__)

RETRYABLE_STATUSES = frozenset({404, 408, 429, 500, 502, 503, 504})


class MasLoginError(RuntimeError):
    """Raised when the MAS login could not be completed after exhausting retries."""


class MasLoginPermanentError(MasLoginError):
    """Raised when MAS rejected the login in a way that retrying cannot fix."""


def _parse_retry_after(headers: Mapping[str, str] | None) -> float | None:
    if not headers:
        return None
    raw = headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return None


async def post_login_with_retry(
    session: aiohttp.ClientSession,
    mas_url: str,
    payload: dict[str, Any],
    *,
    max_attempts: int = 10,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    timeout: float = 30.0,
    on_retry: Callable[[int, BaseException], None] | None = None,
    sleep=asyncio.sleep,
    rand=random.random,
) -> dict[str, Any]:
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    if base_delay < 0 or max_delay < 0:
        raise ValueError("base_delay and max_delay must be >= 0")
    if timeout <= 0:
        raise ValueError("timeout must be > 0")

    url = f"{mas_url.rstrip('/')}/_matrix/client/v3/login"
    client_timeout = aiohttp.ClientTimeout(total=timeout)
    last_error: MasLoginError | None = None

    for attempt in range(1, max_attempts + 1):
        retry_after: float | None = None
        try:
            async with session.post(url, json=payload, timeout=client_timeout) as resp:
                if resp.status == 200:
                    try:
                        data = await resp.json()
                    except ValueError as exc:
                        last_error = MasLoginError(f"MAS login returned invalid JSON: {exc!r}")
                    else:
                        if isinstance(data, dict) and data.get("access_token"):
                            logger.info(
                                "MAS login succeeded on attempt %d/%d", attempt, max_attempts
                            )
                            return data
                        last_error = MasLoginError("MAS login response missing access_token")
                else:
                    body = await resp.text()
                    if resp.status not in RETRYABLE_STATUSES:
                        raise MasLoginPermanentError(
                            f"MAS login failed ({resp.status}): {body}"
                        )
                    last_error = MasLoginError(f"MAS login failed ({resp.status}): {body}")
                    retry_after = _parse_retry_after(getattr(resp, "headers", None))
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            last_error = MasLoginError(f"MAS login request error: {exc!r}")

        if attempt >= max_attempts:
            break

        delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
        delay = delay / 2 + rand() * (delay / 2)
        if retry_after is not None:
            delay = min(max_delay, max(delay, retry_after))
        logger.warning(
            "MAS login attempt %d/%d failed (%s); retrying in %.1fs",
            attempt,
            max_attempts,
            last_error,
            delay,
        )
        if on_retry is not None:
            on_retry(attempt, last_error)
        await sleep(delay)

    raise last_error or MasLoginError("MAS login failed")


__all__ = ["MasLoginError", "MasLoginPermanentError", "post_login_with_retry"]
