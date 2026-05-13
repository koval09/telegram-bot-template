"""Generic retry/backoff helper.

Single implementation used wherever the design mandates retries:
- Req 1.4 — registration upsert (DB).
- Req 6.6 — audit log write.
- Req 17.1 — Telegram Bot API calls.

Telegram's ``TelegramRetryAfter`` is handled specially: the server tells us how
long to wait, so we sleep that amount and DO NOT consume one of our retry
attempts.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import TypeVar

import structlog

T = TypeVar("T")

log = structlog.get_logger(__name__)


class RetryExhausted(Exception):
    """Raised after ``attempts`` retries all failed with a retryable error."""

    def __init__(self, last_error: BaseException, attempts: int) -> None:
        super().__init__(f"all {attempts} retry attempts failed: {last_error!r}")
        self.last_error = last_error
        self.attempts = attempts


def _telegram_retry_after_cls() -> type[BaseException] | None:
    """Return TelegramRetryAfter class if aiogram is importable, else None."""
    try:
        from aiogram.exceptions import TelegramRetryAfter  # type: ignore

        return TelegramRetryAfter
    except Exception:
        return None


async def with_retry(
    fn: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    delays: Sequence[float] = (1.0, 2.0, 4.0),
    retry_on: tuple[type[BaseException], ...],
    op_name: str = "op",
    _retry_after_budget: int = 5,
) -> T:
    """Run ``fn`` with up to ``attempts`` retries.

    Retryable exceptions listed in ``retry_on`` count against ``attempts``.
    ``TelegramRetryAfter`` does NOT consume an attempt — we sleep for the
    server-provided time and repeat the same attempt, bounded by
    ``_retry_after_budget`` to prevent infinite loops.
    """
    if attempts < 1:
        raise ValueError("attempts must be >= 1")

    retry_after_cls = _telegram_retry_after_cls()
    last_error: BaseException | None = None
    retry_after_seen = 0

    attempt = 1
    while attempt <= attempts:
        try:
            return await fn()
        except BaseException as exc:
            # Handle Telegram's rate limit first (does not consume an attempt).
            if retry_after_cls is not None and isinstance(exc, retry_after_cls):
                retry_after_seen += 1
                if retry_after_seen > _retry_after_budget:
                    log.error(
                        "retry.telegram_retry_after_budget_exceeded",
                        op=op_name,
                        budget=_retry_after_budget,
                    )
                    raise
                wait = float(getattr(exc, "retry_after", 1.0))
                log.warning(
                    "retry.telegram_retry_after",
                    op=op_name,
                    attempt=attempt,
                    wait_seconds=wait,
                )
                await asyncio.sleep(wait)
                continue  # same attempt number

            if isinstance(exc, retry_on):
                last_error = exc
                log.warning(
                    "retry.attempt_failed",
                    op=op_name,
                    attempt=attempt,
                    of=attempts,
                    error=repr(exc),
                )
                if attempt < attempts:
                    idx = min(attempt - 1, len(delays) - 1)
                    await asyncio.sleep(delays[idx])
                    attempt += 1
                    continue
                break

            # Non-retryable — bubble up unchanged.
            raise

    assert last_error is not None
    raise RetryExhausted(last_error, attempts)


# --------------------------------------------------------------------------
# Preset retryable-exception tuples.
# --------------------------------------------------------------------------

def telegram_retryable_exceptions() -> tuple[type[BaseException], ...]:
    """Network/server errors for which we retry Telegram Bot API calls."""
    from aiogram.exceptions import TelegramNetworkError, TelegramServerError

    return (TelegramNetworkError, TelegramServerError)


def db_retryable_exceptions() -> tuple[type[BaseException], ...]:
    """Transient DB errors (connection loss, timeout)."""
    from sqlalchemy.exc import (
        DBAPIError,
        OperationalError,
    )
    from sqlalchemy.exc import (
        TimeoutError as SATimeoutError,
    )

    return (OperationalError, DBAPIError, SATimeoutError)
