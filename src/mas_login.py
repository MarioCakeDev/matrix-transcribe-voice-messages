import asyncio
import logging
import random
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

RETRYABLE_STATUSES = frozenset({404, 408, 429, 500, 502, 503, 504})


class MasLoginError(RuntimeError):
    """Raised when the MAS login could not be completed after exhausting retries."""


class MasLoginPermanentError(MasLoginError):
    """Raised when MAS rejected the login in a way that retrying cannot fix."""


async def post_login_with_retry(
    session: aiohttp.ClientSession,
    mas_url: str,
    payload: dict[str, Any],
    *,
    max_attempts: int = 10,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    timeout: float = 30.0,
    sleep=asyncio.sleep,
    rand=random.random,
) -> dict[str, Any]:
    url = f"{mas_url.rstrip('/')}/_matrix/client/v3/login"
    client_timeout = aiohttp.ClientTimeout(total=timeout)
    last_error: MasLoginError | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            async with session.post(url, json=payload, timeout=client_timeout) as resp:
                if resp.status == 200:
                    logger.info("MAS login succeeded on attempt %d/%d", attempt, max_attempts)
                    return await resp.json()
                body = await resp.text()
                if resp.status not in RETRYABLE_STATUSES:
                    raise MasLoginPermanentError(
                        f"MAS login failed ({resp.status}): {body}"
                    )
                last_error = MasLoginError(f"MAS login failed ({resp.status}): {body}")
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            last_error = MasLoginError(f"MAS login request error: {exc!r}")

        if attempt >= max_attempts:
            break

        delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
        delay = delay / 2 + rand() * (delay / 2)
        logger.warning(
            "MAS login attempt %d/%d failed (%s); retrying in %.1fs",
            attempt,
            max_attempts,
            last_error,
            delay,
        )
        await sleep(delay)

    raise last_error or MasLoginError("MAS login failed")


__all__ = ["MasLoginError", "MasLoginPermanentError", "post_login_with_retry"]
