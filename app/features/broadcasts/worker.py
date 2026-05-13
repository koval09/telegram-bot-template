"""Модуль_Рассылки — consumer side (task 22.2).

The :class:`BroadcastWorker` is a single background asyncio task that drains
``bcast:queue`` written by :mod:`app.features.broadcasts.producer`. For each
job it:

1. Persists a new ``broadcasts`` row with ``status=running`` and
   ``started_at=now`` (Req 8.1).
2. Streams matching recipients via :meth:`UsersRepo.iterate_for_broadcast`,
   which always excludes banned users and users who blocked the bot.
3. Sends the text via :meth:`aiogram.Bot.send_message` while honouring a
   token-bucket rate limit of 30 req/s (Req 8.2).
4. Handles Telegram failures:

   * ``TelegramForbiddenError`` → mark the user via
     :meth:`UsersRepo.mark_blocked_bot` and bump the ``blocked`` counter
     (Req 8.3);
   * ``TelegramRetryAfter`` → sleep for the server-provided time and
     retry the **same** recipient without counting it as ``failed``;
   * any other exception → bump the ``failed`` counter and write
     ``audit.record_error(source="Telegram API", ...)``.

5. Checks ``bcast:cancel:<id>`` before each send. If the flag is set,
   stops sending within 5 seconds, persists ``status=cancelled`` and
   sends an interim report to the initiator (Req 8.6).
6. On normal completion persists ``status=completed`` + ``finished_at=now``
   and sends the final report
   «всего / доставлено / не доставлено / заблокировали» to the initiator
   (Req 8.4).

Startup / shutdown wiring lives in :mod:`app.__main__`:

* after ``register_routers`` the main coroutine calls
  ``services.broadcasts.worker.start()`` if the bundle exists;
* in the graceful-shutdown path it calls
  ``services.broadcasts.worker.stop(timeout=10)`` before closing Redis.

Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog
from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from redis.asyncio import Redis

from app.core.db.models import BroadcastStatus
from app.core.repositories.users import BroadcastFilter, UsersRepo
from app.core.utils.clock import Clock, utc_now
from app.features.broadcasts.producer import (
    CANCEL_KEY_PREFIX,
    FILTER_ACTIVE_30D,
    FILTER_ALL,
    FILTER_LANG,
    QUEUE_KEY,
)

if TYPE_CHECKING:  # pragma: no cover
    from app.core.repositories.broadcasts import BroadcastsRepo
    from app.core.services.audit import AuditLog

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

# BRPOP poll interval. Short enough that ``stop()`` cancels the loop within a
# handful of seconds even if the queue is empty.
_BRPOP_TIMEOUT_SECONDS = 5

# How often we flush ``total / delivered / failed / blocked`` to the DB.
# Whichever threshold is hit first wins.
_COUNTER_FLUSH_EVERY_N = 50
_COUNTER_FLUSH_EVERY_SECONDS = 10.0

# Supported filter kinds — mirror producer.FILTER_*.
_VALID_FILTER_KINDS = frozenset({FILTER_ALL, FILTER_ACTIVE_30D, FILTER_LANG})

# Russian literals — admin UI is Russian-first per spec. Keys matching
# ``broadcasts.report.*`` are reserved in ru.yml / en.yml so a future refactor
# can swap them for translator lookups without touching call sites.
_MSG_FINAL_REPORT = (
    "Рассылка {id} завершена. "
    "Всего: {t}, доставлено: {d}, не доставлено: {f}, заблокировали: {b}."
)
_MSG_CANCEL_REPORT = (
    "Рассылка {id} отменена. "
    "Доставлено: {d}, не доставлено: {f}, заблокировали: {b}."
)


@dataclass(slots=True)
class _Counters:
    """Mutable per-job counters we flush to ``broadcasts`` periodically."""

    total: int = 0
    delivered: int = 0
    failed: int = 0
    blocked: int = 0


class BroadcastWorker:
    """Single-process background consumer for ``bcast:queue``.

    Constructed by :mod:`app.container` when ``feature_broadcasts=True`` and
    started from :mod:`app.__main__` after the dispatcher is ready. Kept
    intentionally simple: one task, one loop, one Telegram send at a time —
    bursts up to the configured rate limit.
    """

    def __init__(
        self,
        bot: Bot,
        redis: Redis,
        broadcasts_repo: BroadcastsRepo,
        users_repo: UsersRepo,
        audit: AuditLog,
        *,
        rate_limit_per_second: int = 30,
        clock: Clock = utc_now,
    ) -> None:
        if rate_limit_per_second < 1:
            raise ValueError("rate_limit_per_second must be >= 1")
        self._bot = bot
        self._redis = redis
        self._broadcasts = broadcasts_repo
        self._users = users_repo
        self._audit = audit
        # Stored as float so ``asyncio.sleep`` gets a sub-second delay.
        self._tick_seconds = 1.0 / rate_limit_per_second
        self._clock = clock
        self._task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def start(self) -> None:
        """Spawn the background task. Idempotent."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self.run(), name="broadcasts.worker")
        log.info("broadcasts.worker.started")

    async def stop(self, timeout: float = 10.0) -> None:
        """Cancel the background task and wait for it to unwind."""
        task = self._task
        if task is None:
            return
        if not task.done():
            task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except asyncio.CancelledError:
            pass
        except TimeoutError:
            log.warning("broadcasts.worker.stop_timeout", timeout=timeout)
        except Exception as exc:
            log.warning("broadcasts.worker.stop_error", error=repr(exc))
        finally:
            self._task = None
        log.info("broadcasts.worker.stopped")

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------
    async def run(self) -> None:
        """Drain ``bcast:queue`` one job at a time until cancelled."""
        log.info("broadcasts.worker.run_loop_enter")
        try:
            while True:
                try:
                    popped = await self._redis.brpop(
                        QUEUE_KEY, timeout=_BRPOP_TIMEOUT_SECONDS
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.error(
                        "broadcasts.worker.brpop_failed", error=repr(exc)
                    )
                    # Back off a little so a broken Redis does not hot-loop.
                    await asyncio.sleep(1.0)
                    continue

                if popped is None:
                    # BRPOP timed out — just poll again.
                    continue

                # ``BRPOP`` returns ``(key, value)``; we only care about
                # the value. ``redis-py`` decodes bytes → str when
                # ``decode_responses=True`` (our default), but guard
                # against byte payloads too.
                _, raw = popped
                await self._handle_raw_job(raw)
        except asyncio.CancelledError:
            log.info("broadcasts.worker.run_loop_cancelled")
            raise

    # ------------------------------------------------------------------
    # Per-job handling
    # ------------------------------------------------------------------
    async def _handle_raw_job(self, raw: Any) -> None:
        """Parse + dispatch a single queue entry. Never raises."""
        payload = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
        try:
            job = _parse_job(payload)
        except _BadJob as exc:
            log.error(
                "broadcasts.worker.bad_job",
                error=str(exc),
                payload_preview=payload[:200],
            )
            try:
                await self._audit.record_error(
                    source="Модуль_Рассылки",
                    message=f"bad broadcast job: {exc!s}",
                )
            except Exception as audit_exc:
                log.warning(
                    "broadcasts.worker.audit_failed", error=repr(audit_exc)
                )
            return

        try:
            await self._process_job(job)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error(
                "broadcasts.worker.job_failed",
                job_id=job.id,
                created_by=job.created_by,
                error=repr(exc),
            )
            try:
                await self._audit.record_error(
                    source="Модуль_Рассылки",
                    message=f"job {job.id} crashed: {exc!r}",
                    actor_id=job.created_by,
                )
            except Exception as audit_exc:
                log.warning(
                    "broadcasts.worker.audit_failed", error=repr(audit_exc)
                )

    async def _process_job(self, job: _Job) -> None:
        """Fully process one validated job from queue to final report."""
        bf = BroadcastFilter(kind=job.filter_kind, value=job.filter_value)
        broadcast = await self._broadcasts.create_running(
            created_by=job.created_by,
            text=job.text,
            filter_kind=job.filter_kind,
            filter_value=job.filter_value,
            now=self._clock(),
        )
        log.info(
            "broadcasts.worker.job_started",
            job_id=job.id,
            broadcast_id=broadcast.id,
            created_by=job.created_by,
            filter_kind=job.filter_kind,
            filter_value=job.filter_value,
        )

        counters = _Counters()
        last_flush_at = asyncio.get_event_loop().time()
        since_last_flush = 0
        cancelled = False
        cancel_key = f"{CANCEL_KEY_PREFIX}{job.id}"

        async for telegram_id in self._users.iterate_for_broadcast(bf):
            # Check cancel flag BEFORE each send (Req 8.6). BRPOP already
            # costs up to 5s idle, so checking here keeps the cancellation
            # window inside the 5-second budget.
            if await self._cancel_flag_set(cancel_key):
                cancelled = True
                log.info(
                    "broadcasts.worker.cancelled",
                    job_id=job.id,
                    broadcast_id=broadcast.id,
                    processed=counters.total,
                )
                break

            counters.total += 1
            await self._send_to_user(telegram_id, job.text, counters)

            # Pace ourselves to stay under the 30 req/s Telegram ceiling
            # (Req 8.2). Sleeping after each send gives a simple token
            # bucket with bucket_size=1.
            await asyncio.sleep(self._tick_seconds)

            # Flush counters every N sends or every T seconds so the DB
            # reflects progress even for long-running jobs.
            since_last_flush += 1
            now_mono = asyncio.get_event_loop().time()
            if (
                since_last_flush >= _COUNTER_FLUSH_EVERY_N
                or now_mono - last_flush_at >= _COUNTER_FLUSH_EVERY_SECONDS
            ):
                await self._flush_counters(broadcast.id, counters)
                since_last_flush = 0
                last_flush_at = now_mono

        # Final counter flush (for both cancelled and completed runs).
        await self._flush_counters(broadcast.id, counters)

        finish_now = self._clock()
        if cancelled:
            await self._broadcasts.finish(
                broadcast.id, BroadcastStatus.cancelled, finish_now
            )
            await self._send_report(
                job.created_by,
                _MSG_CANCEL_REPORT.format(
                    id=job.id,
                    d=counters.delivered,
                    f=counters.failed,
                    b=counters.blocked,
                ),
            )
            log.info(
                "broadcasts.worker.job_cancelled",
                job_id=job.id,
                broadcast_id=broadcast.id,
                total=counters.total,
                delivered=counters.delivered,
                failed=counters.failed,
                blocked=counters.blocked,
            )
            return

        await self._broadcasts.finish(
            broadcast.id, BroadcastStatus.completed, finish_now
        )
        await self._send_report(
            job.created_by,
            _MSG_FINAL_REPORT.format(
                id=job.id,
                t=counters.total,
                d=counters.delivered,
                f=counters.failed,
                b=counters.blocked,
            ),
        )
        log.info(
            "broadcasts.worker.job_completed",
            job_id=job.id,
            broadcast_id=broadcast.id,
            total=counters.total,
            delivered=counters.delivered,
            failed=counters.failed,
            blocked=counters.blocked,
        )

    # ------------------------------------------------------------------
    # Send path — per-recipient
    # ------------------------------------------------------------------
    async def _send_to_user(
        self, telegram_id: int, text: str, counters: _Counters
    ) -> None:
        """Send ``text`` to ``telegram_id`` with RetryAfter-aware retries.

        Outcomes (exactly one of these fires per call):

        * success → ``counters.delivered += 1``;
        * ``TelegramForbiddenError`` → mark blocked, ``blocked += 1``;
        * any other exception → ``failed += 1`` + audit record.

        ``TelegramRetryAfter`` is handled inside the inner loop: we sleep
        the server-provided duration and try again for the **same** user.
        A small budget prevents an infinite loop if Telegram keeps sending
        429s back.
        """
        retry_after_budget = 5
        while True:
            try:
                await self._bot.send_message(telegram_id, text)
            except TelegramRetryAfter as exc:
                retry_after_budget -= 1
                wait = float(getattr(exc, "retry_after", 1.0))
                log.warning(
                    "broadcasts.worker.retry_after",
                    telegram_id=telegram_id,
                    wait_seconds=wait,
                    budget_left=retry_after_budget,
                )
                if retry_after_budget < 0:
                    counters.failed += 1
                    try:
                        await self._audit.record_error(
                            source="Telegram API",
                            message=(
                                f"retry_after budget exhausted for"
                                f" {telegram_id}: {exc!r}"
                            ),
                            actor_id=telegram_id,
                        )
                    except Exception as audit_exc:
                        log.warning(
                            "broadcasts.worker.audit_failed",
                            error=repr(audit_exc),
                        )
                    return
                await asyncio.sleep(wait)
                continue
            except TelegramForbiddenError:
                counters.blocked += 1
                try:
                    await self._users.mark_blocked_bot(telegram_id)
                except Exception as exc:
                    log.warning(
                        "broadcasts.worker.mark_blocked_failed",
                        telegram_id=telegram_id,
                        error=repr(exc),
                    )
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                counters.failed += 1
                try:
                    await self._audit.record_error(
                        source="Telegram API",
                        message=(
                            f"broadcast send failed to {telegram_id}: {exc!r}"
                        ),
                        actor_id=telegram_id,
                    )
                except Exception as audit_exc:
                    log.warning(
                        "broadcasts.worker.audit_failed",
                        error=repr(audit_exc),
                    )
                return
            else:
                counters.delivered += 1
                return

    # ------------------------------------------------------------------
    # Small helpers
    # ------------------------------------------------------------------
    async def _cancel_flag_set(self, cancel_key: str) -> bool:
        try:
            return bool(await self._redis.exists(cancel_key))
        except Exception as exc:
            log.warning(
                "broadcasts.worker.cancel_check_failed",
                key=cancel_key,
                error=repr(exc),
            )
            return False

    async def _flush_counters(
        self, broadcast_id: int, counters: _Counters
    ) -> None:
        try:
            await self._broadcasts.update_counters(
                broadcast_id,
                total=counters.total,
                delivered=counters.delivered,
                failed=counters.failed,
                blocked=counters.blocked,
            )
        except Exception as exc:
            log.warning(
                "broadcasts.worker.flush_failed",
                broadcast_id=broadcast_id,
                error=repr(exc),
            )

    async def _send_report(self, chat_id: int, text: str) -> None:
        """Best-effort report delivery — never raises back to the caller."""
        try:
            await self._bot.send_message(chat_id, text)
        except Exception as exc:
            log.warning(
                "broadcasts.worker.report_failed",
                chat_id=chat_id,
                error=repr(exc),
            )


# ---------------------------------------------------------------------------
# Job parsing
# ---------------------------------------------------------------------------


class _BadJob(ValueError):
    """Raised by :func:`_parse_job` when the queue payload is malformed."""


@dataclass(slots=True)
class _Job:
    id: str
    created_by: int
    text: str
    filter_kind: str
    filter_value: str | None


def _parse_job(payload: str) -> _Job:
    """Parse and validate the JSON payload written by the producer.

    Contract — see :mod:`app.features.broadcasts.producer`:

        {"id": <uuid>, "created_by": <int>, "text": <str>,
         "filter": {"kind": "all|active_30d|lang", "value": str|null},
         "created_at": <iso8601>}
    """
    try:
        obj: Any = json.loads(payload)
    except ValueError as exc:
        raise _BadJob(f"invalid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise _BadJob("payload is not a JSON object")

    job_id = obj.get("id")
    created_by = obj.get("created_by")
    text = obj.get("text")
    filt = obj.get("filter")

    if not isinstance(job_id, str) or not job_id:
        raise _BadJob("missing or invalid 'id'")
    if not isinstance(created_by, int):
        raise _BadJob("missing or invalid 'created_by'")
    if not isinstance(text, str) or not text:
        raise _BadJob("missing or invalid 'text'")
    if not isinstance(filt, dict):
        raise _BadJob("missing or invalid 'filter'")

    kind = filt.get("kind")
    value = filt.get("value")
    if kind not in _VALID_FILTER_KINDS:
        raise _BadJob(f"unknown filter kind: {kind!r}")
    if kind == FILTER_LANG:
        if not isinstance(value, str) or not value:
            raise _BadJob("filter kind 'lang' requires a non-empty value")
    else:
        if value is not None and not isinstance(value, str):
            raise _BadJob("filter value must be string or null")
        value = None

    return _Job(
        id=job_id,
        created_by=created_by,
        text=text,
        filter_kind=kind,
        filter_value=value,
    )


__all__ = ["BroadcastWorker"]
