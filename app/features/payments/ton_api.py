"""Thin TonCenter v3 client used to verify incoming TON payments (task 26.1).

Covers the polling side of :class:`TonPaymentsService` (Req 13.2 / 13.3):

* :meth:`TonApiClient.get_incoming_transactions` — pulls inbound transfers
  to a given address since ``after`` using TonCenter's ``v3/transactions``
  endpoint (``/api/v3/transactions?account=<addr>&start_utime=<ts>&sort=asc``).
* :meth:`TonApiClient.find_by_payload` — iterates that list and returns the
  first transaction whose decoded text-comment equals ``payload``.

Endpoint choice — **TonCenter v3**
----------------------------------
We pick TonCenter over TonAPI because:

* It is fully free-tier with optional ``X-Api-Key`` for higher rate limits;
  the project already pins ``ton_api_key`` in :class:`app.config.Settings`
  as a ``SecretStr`` (used here via the ``Authorization`` header).
* The v3 API decodes ``message.body`` into a ``message_content.decoded``
  block for text comments, so we do not need to parse BoCs manually —
  matching ``payload`` becomes a trivial string compare.
* Response shape is stable and well-documented:
  https://toncenter.com/api/v3/

Retries
-------
Every HTTP request goes through :func:`app.core.services.retry.with_retry`
with ``attempts=3`` and ``delays=(1, 2, 4)``. Retryable exceptions are
``aiohttp.ClientError`` and ``asyncio.TimeoutError`` (Req 13.2 / 17.1).
``with_retry`` raises :class:`~app.core.services.retry.RetryExhausted` when
all attempts fail — callers translate that into an ``audit.record_error``
and skip this poll round (task 26.2 poller handles the cadence).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import aiohttp
import structlog

from app.core.services.retry import with_retry

if TYPE_CHECKING:  # pragma: no cover — typing only
    from collections.abc import AsyncIterator


log = structlog.get_logger(__name__)


# Public data shape — kept intentionally small so downstream code (task 26.2)
# only touches what it needs.
@dataclass(frozen=True, slots=True)
class TonTx:
    """One inbound TON transaction as seen by the polling job.

    Attributes:
        hash: Transaction hash (hex lowercase — TonCenter normalizes).
        amount_nano: Value credited to ``address`` in nanoTON. Guaranteed
            non-negative; zero-value service transactions are skipped in
            :meth:`TonApiClient.get_incoming_transactions`.
        payload: Decoded text comment ("body.comment" / "decoded.comment")
            if present, else ``None``. The polling job matches
            ``payload_id`` against this field.
        timestamp: Transaction ``utime`` as timezone-aware UTC.
        source: Sender address in user-friendly (bounceable) format when
            TonCenter provides it — otherwise the raw ``<wc>:<hex>``
            string. ``None`` for external-in messages.
    """

    hash: str
    amount_nano: int
    payload: str | None
    timestamp: datetime
    source: str | None


# HTTP retry policy for the client — exported so the scheduler task
# (26.2) can reuse the same settings when it wraps higher-level calls.
DEFAULT_RETRY_ATTEMPTS = 3
DEFAULT_RETRY_DELAYS: tuple[float, ...] = (1.0, 2.0, 4.0)

# TonCenter's default page size is 128; we cap at 256 so a single poll
# can cover a reasonably busy receive address without multiple round-trips.
_DEFAULT_LIMIT = 256


class TonApiClient:
    """Thin aiohttp wrapper around TonCenter v3.

    Not thread-safe: share a single instance per event loop. The client
    does NOT own its :class:`aiohttp.ClientSession` — the caller
    (typically :mod:`app.container`) creates one with the lifetime of the
    bot process so sockets are reused.

    Args:
        http_session: Caller-owned :class:`aiohttp.ClientSession`. Closed
            by the container on shutdown.
        base_url: Root URL of the TonCenter v3 API, e.g.
            ``https://toncenter.com`` (``settings.ton_api_url``). Passed
            through :func:`str` so :class:`pydantic.HttpUrl` can be used
            at the call site without conversion.
        api_key: Optional ``X-Api-Key`` / ``Authorization`` token for
            TonCenter's higher rate-limit tier.
        timeout: Per-request timeout (seconds).
        retry_attempts: Maximum retry attempts across transient failures.
        retry_delays: Delay schedule between retries (seconds).
    """

    def __init__(
        self,
        http_session: aiohttp.ClientSession,
        base_url: str,
        *,
        api_key: str | None = None,
        timeout: float = 10.0,
        retry_attempts: int = DEFAULT_RETRY_ATTEMPTS,
        retry_delays: tuple[float, ...] = DEFAULT_RETRY_DELAYS,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self._session = http_session
        self._base_url = str(base_url).rstrip("/")
        self._api_key = api_key
        self._timeout = aiohttp.ClientTimeout(total=float(timeout))
        self._retry_attempts = int(retry_attempts)
        self._retry_delays = tuple(retry_delays)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def get_incoming_transactions(
        self,
        address: str,
        after: datetime,
        *,
        limit: int = _DEFAULT_LIMIT,
    ) -> list[TonTx]:
        """Return inbound transactions to ``address`` since ``after`` UTC.

        Only transactions that have an ``in_msg`` whose destination
        matches ``address`` (TonCenter already filters by account, so we
        additionally gate on a non-empty source — external-in service
        messages are skipped) and a positive value are returned. Missing
        or malformed text comments are tolerated: the ``payload`` field
        is set to ``None`` so the caller can still reason about the
        amount/hash.

        Transactions are returned sorted by ``timestamp`` ascending so
        ``find_by_payload`` can stop at the first match (= earliest)
        without scanning the tail.
        """
        after_utc = _to_utc(after)
        params: dict[str, Any] = {
            "account": address,
            "start_utime": int(after_utc.timestamp()),
            "limit": int(limit),
            "offset": 0,
            "sort": "asc",
        }

        data = await self._request_json("/api/v3/transactions", params=params)

        raw_txs = data.get("transactions") or []
        result: list[TonTx] = []
        for raw in raw_txs:
            parsed = _parse_transaction(raw, destination=address)
            if parsed is not None:
                result.append(parsed)
        return result

    async def find_by_payload(
        self,
        address: str,
        payload: str,
        after: datetime,
        *,
        limit: int = _DEFAULT_LIMIT,
    ) -> TonTx | None:
        """Return the first tx to ``address`` whose text comment matches ``payload``.

        Scans :meth:`get_incoming_transactions` in ascending timestamp
        order; returns the first match or ``None``. The ``payload`` field
        is compared byte-for-byte after stripping surrounding whitespace
        (TonCenter occasionally includes a trailing newline from the
        on-chain body cell).
        """
        if not payload:
            raise ValueError("payload must be non-empty")
        target = payload.strip()

        # Streaming would require pagination; for a 15-minute window
        # 256 transactions is already the worst-case for a busy receive
        # address. If higher scale is ever needed the loop can be
        # replaced with an offset-driven generator.
        txs = await self.get_incoming_transactions(address, after, limit=limit)
        for tx in txs:
            if tx.payload is not None and tx.payload.strip() == target:
                return tx
        return None

    async def iter_incoming_transactions(
        self,
        address: str,
        after: datetime,
        *,
        limit: int = _DEFAULT_LIMIT,
    ) -> AsyncIterator[TonTx]:
        """Async-iterate over :meth:`get_incoming_transactions`.

        Convenience helper for callers that want to early-exit after
        processing the first few hits. Does not paginate beyond the first
        page — for the 15-minute window required by the scheduler task
        that is always sufficient.
        """
        for tx in await self.get_incoming_transactions(
            address, after, limit=limit
        ):
            yield tx

    # ------------------------------------------------------------------
    # Internal HTTP
    # ------------------------------------------------------------------
    async def _request_json(
        self, path: str, *, params: dict[str, Any]
    ) -> dict[str, Any]:
        """HTTP GET with retries; returns decoded JSON dict.

        Wraps the actual call in :func:`with_retry` so transient network
        failures don't bring down the poller.
        """
        url = self._base_url + path
        headers: dict[str, str] = {"Accept": "application/json"}
        if self._api_key:
            # TonCenter accepts both ``X-Api-Key`` and ``Authorization``;
            # ``X-Api-Key`` is the older v2 name that still works in v3.
            headers["X-Api-Key"] = self._api_key

        async def _do() -> dict[str, Any]:
            async with self._session.get(
                url,
                params=params,
                headers=headers,
                timeout=self._timeout,
            ) as resp:
                if resp.status >= 500:
                    # Raise ClientError so with_retry considers it
                    # retryable; the body is echoed for audit context.
                    body_preview = (await resp.text())[:200]
                    raise aiohttp.ClientResponseError(
                        resp.request_info,
                        resp.history,
                        status=resp.status,
                        message=f"toncenter 5xx: {body_preview}",
                        headers=resp.headers,
                    )
                resp.raise_for_status()
                payload = await resp.json(content_type=None)
                if not isinstance(payload, dict):
                    raise aiohttp.ClientPayloadError(
                        f"unexpected toncenter response: {type(payload).__name__}"
                    )
                return payload

        return await with_retry(
            _do,
            attempts=self._retry_attempts,
            delays=self._retry_delays,
            retry_on=(aiohttp.ClientError, asyncio.TimeoutError),
            op_name="toncenter.get_transactions",
        )


# ---------------------------------------------------------------------------
# Parsing helpers — kept module-level so they're unit-testable.
# ---------------------------------------------------------------------------


def _to_utc(dt: datetime) -> datetime:
    """Normalize a ``datetime`` to aware UTC.

    Naive datetimes are assumed to already be UTC (the project uses
    :func:`app.core.utils.clock.utc_now` everywhere); aware datetimes in
    another zone are converted.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _parse_transaction(raw: dict[str, Any], *, destination: str) -> TonTx | None:
    """Translate a TonCenter v3 transaction record into :class:`TonTx`.

    Returns ``None`` for transactions we cannot use (no ``in_msg``,
    zero or missing ``value``, non-credit direction, malformed hash).
    Never raises — callers already filtered by account on the server
    side, so an unexpected shape means "skip this row" rather than
    "abort the whole poll".
    """
    try:
        in_msg = raw.get("in_msg") or {}
        if not in_msg:
            return None

        value_raw = in_msg.get("value")
        if value_raw is None:
            return None
        amount_nano = int(value_raw)
        if amount_nano <= 0:
            return None

        tx_hash_raw = raw.get("hash")
        if not tx_hash_raw:
            return None
        tx_hash = str(tx_hash_raw).lower()

        utime_raw = raw.get("now") or raw.get("utime")
        if utime_raw is None:
            return None
        ts = datetime.fromtimestamp(int(utime_raw), tz=UTC)

        # Optional bits.
        source = in_msg.get("source")
        if isinstance(source, dict):
            source = source.get("address") or source.get("user_friendly")
        source_str = str(source) if source else None

        payload = _extract_text_comment(in_msg)

        return TonTx(
            hash=tx_hash,
            amount_nano=amount_nano,
            payload=payload,
            timestamp=ts,
            source=source_str,
        )
    except (TypeError, ValueError, KeyError) as exc:
        log.warning(
            "ton_api.parse_failed",
            error=repr(exc),
            destination=destination,
            hash=str(raw.get("hash"))[:64],
        )
        return None


def _extract_text_comment(in_msg: dict[str, Any]) -> str | None:
    """Extract the decoded text comment from an ``in_msg`` if present.

    TonCenter v3 exposes the decoded body under
    ``message_content.decoded``: a dict with ``type=="text_comment"`` and
    ``comment`` as the plain-text body. Older responses / alternate
    providers may surface it as ``decoded_body`` or ``comment`` directly —
    we try all known spellings and return ``None`` if none match.
    """
    # Preferred path — v3 ``message_content.decoded``.
    mc = in_msg.get("message_content") or {}
    decoded = mc.get("decoded") if isinstance(mc, dict) else None
    if isinstance(decoded, dict):
        if decoded.get("type") in ("text_comment", "comment"):
            comment = decoded.get("comment")
            if isinstance(comment, str):
                return comment

    # Legacy / alternate keys.
    for key in ("decoded_body", "comment", "text_comment"):
        value = in_msg.get(key)
        if isinstance(value, str) and value:
            return value

    # Some builds nest the comment under ``body.comment``.
    body = in_msg.get("body")
    if isinstance(body, dict):
        comment = body.get("comment")
        if isinstance(comment, str):
            return comment

    return None


__all__ = [
    "DEFAULT_RETRY_ATTEMPTS",
    "DEFAULT_RETRY_DELAYS",
    "TonApiClient",
    "TonTx",
]
