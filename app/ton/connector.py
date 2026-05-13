"""TON_Коннектор — wraps ``pytonconnect`` and ties it to our repos + audit.

Implements the three high-level operations required by the Stage-3 handlers
(task 14.3) and the cleanup scheduler (task 15.2):

* :meth:`TonConnector.start_connection` — build a fresh TON Connect 2 session,
  produce the wallet deeplink + QR, persist a one-time nonce under
  ``tc:nonce:{telegram_id}``. Requirement 3.1.
* :meth:`TonConnector.await_connection` — wait for the wallet callback,
  verify ``ton_proof`` via :func:`app.ton.verifier.verify_proof`, persist
  ``ton_address/ton_wallet_name/ton_connected_at`` on success (one active
  wallet per user), audit every failure path. Requirements 3.2, 3.3, 3.6.
* :meth:`TonConnector.disconnect` — close the ``pytonconnect`` session, clear
  wallet fields, and wipe every ``tc:session:{telegram_id}:*`` / ``tc:nonce``
  key so the user can start again. Requirement 3.4.

:class:`RedisSessionStore` refreshes its TTL on every write (Requirement 3.5);
:meth:`TonConnector.list_active_sessions` is used by the ``tc_session_cleanup``
APScheduler job to find expired sessions.

Keeping ``pytonconnect`` and ``qrcode`` as lazy imports lets the rest of the
app load cleanly when the feature flag is off or the optional deps are
missing. The QR step gracefully degrades to an empty string if ``qrcode``
is not available — the deeplink alone is enough to complete the flow.
"""

from __future__ import annotations

import asyncio
import base64
import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Union

import structlog
from pytoniq_core import Address

from app.core.utils.clock import Clock, utc_now
from app.ton.session_store import RedisSessionStore
from app.ton.verifier import InvalidProof, TonProof, verify_proof

if TYPE_CHECKING:  # pragma: no cover - typing only
    from redis.asyncio import Redis

    from app.config import Settings
    from app.core.repositories.users import UsersRepo
    from app.core.services.audit import AuditLog


log = structlog.get_logger(__name__)


_NONCE_KEY_PREFIX = "tc:nonce"
_SESSION_KEY_PREFIX = "tc:session"
_CONNECT_META_KEY_PREFIX = "tc:connect_meta"
# Grace window on top of ``session_ttl_seconds`` so that ``tc_session_cleanup``
# can still read chat/message metadata after the session keys have expired.
_CONNECT_META_GRACE_SECONDS = 120


# ---------------------------------------------------------------------------
# Public data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StartResult:
    """Result of :meth:`TonConnector.start_connection`.

    ``qr_base64`` is an empty string when ``qrcode`` is not installed —
    handlers should fall back to ``deeplink`` only in that case.
    """

    deeplink: str
    qr_base64: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ConnectMeta:
    """Chat/message metadata for the outbound ``/connect_wallet`` message.

    Persisted under ``tc:connect_meta:{telegram_id}`` right after the deeplink
    is sent so that the ``ton_session_cleanup`` scheduler job (task 15.2) can
    edit the user's original message once the TON Connect session expires
    (Requirement 3.5). The hash outlives the ``tc:session:{id}:*`` keys by a
    small grace window so we still have ``chat_id``/``message_id`` available
    at the moment Redis TTL has already evicted the session state.
    """

    telegram_id: int
    chat_id: int
    message_id: int
    kind: str  # "photo" or "text"
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ConnectionSuccess:
    """Successful ``await_connection`` outcome."""

    address: str
    wallet_name: str | None
    connected_at: datetime


@dataclass(frozen=True, slots=True)
class ConnectionFailure:
    """Failed ``await_connection`` outcome.

    ``reason`` is one of the verifier's tags (``"payload_shape"``,
    ``"telegram_id_mismatch"``, ``"timestamp_out_of_window"``, ``"nonce"``,
    ``"signature"`` ...), ``"timeout"`` when the user did not finish within
    ``connect_timeout_seconds``, ``"no_session"`` when nothing was started
    for the telegram_id, or ``"sdk_error"`` for anything else.
    """

    reason: str


ConnectionResult = Union[ConnectionSuccess, ConnectionFailure]


class AlreadyConnectedError(Exception):
    """Raised when the user already has ``ton_address`` persisted.

    Requirement 3.6: one active wallet per user. Handlers catch this and
    prompt the user to run ``/disconnect_wallet`` first.
    """


# ---------------------------------------------------------------------------
# Connector
# ---------------------------------------------------------------------------


class TonConnector:
    """TON Connect 2 façade used by ``/connect_wallet`` and ``/disconnect_wallet``."""

    def __init__(
        self,
        redis: Redis,
        users_repo: UsersRepo,
        audit: AuditLog,
        settings: Settings,
        *,
        session_ttl_seconds: int = 600,
        connect_timeout_seconds: int = 600,
        clock: Clock = utc_now,
    ) -> None:
        if session_ttl_seconds <= 0:
            raise ValueError("session_ttl_seconds must be positive")
        if connect_timeout_seconds <= 0:
            raise ValueError("connect_timeout_seconds must be positive")
        self._redis = redis
        self._users_repo = users_repo
        self._audit = audit
        self._settings = settings
        self._session_ttl_seconds = int(session_ttl_seconds)
        self._connect_timeout_seconds = int(connect_timeout_seconds)
        self._clock = clock
        # In-process cache of live ``pytonconnect.TonConnect`` instances, keyed
        # by telegram_id. A handler calls ``start_connection`` then hands off
        # to a background task that calls ``await_connection`` — both need the
        # same connector object, so we hold it here until either
        # ``await_connection`` completes or ``disconnect`` is called.
        self._connectors: dict[int, Any] = {}

    # ------------------------------------------------------------------
    # start_connection
    # ------------------------------------------------------------------
    async def start_connection(self, telegram_id: int) -> StartResult:
        """Kick off a TON Connect 2 session for ``telegram_id``.

        Requirement 3.1. Raises :class:`AlreadyConnectedError` when the
        user already has a wallet bound (Requirement 3.6).
        """
        user = await self._users_repo.get_by_tg_id(telegram_id)
        if user is not None and getattr(user, "ton_address", None):
            raise AlreadyConnectedError(
                f"telegram_id={telegram_id} already has a connected wallet"
            )

        # Lazy SDK import — feature-gated.
        from pytonconnect import TonConnect

        now = self._clock()
        nonce = secrets.token_urlsafe(32)
        await self._redis.set(
            f"{_NONCE_KEY_PREFIX}:{telegram_id}",
            nonce,
            ex=self._session_ttl_seconds,
        )

        storage = RedisSessionStore(
            self._redis, telegram_id, ttl=self._session_ttl_seconds
        )
        tc = TonConnect(str(self._settings.ton_manifest_url), storage)
        self._connectors[telegram_id] = tc

        wallets = await tc.get_wallets()
        payload = f"tg:{telegram_id}:{int(now.timestamp())}:{nonce}"
        deeplink: str = await tc.connect(wallets, request={"tonProof": payload})

        qr_base64 = self._render_qr(deeplink)
        expires_at = now + timedelta(seconds=self._session_ttl_seconds)
        return StartResult(
            deeplink=deeplink, qr_base64=qr_base64, expires_at=expires_at
        )

    @staticmethod
    def _render_qr(deeplink: str) -> str:
        """Render ``deeplink`` as a base64 PNG QR code.

        Returns ``""`` if ``qrcode`` is not installed — handlers should
        fall back to showing the deeplink as a clickable URL instead.
        """
        try:
            import qrcode  # type: ignore[import-not-found]
        except ImportError:
            log.warning(
                "tc_qr_library_missing",
                detail="install qrcode[pil] to render connection QR codes",
            )
            return ""
        from io import BytesIO

        img = qrcode.make(deeplink)
        buf = BytesIO()
        img.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode("ascii")

    # ------------------------------------------------------------------
    # await_connection
    # ------------------------------------------------------------------
    async def await_connection(self, telegram_id: int) -> ConnectionResult:
        """Wait for the wallet approve-callback and persist the binding.

        Requirements 3.2, 3.3, 3.6. Always pops the cached connector so a
        subsequent ``start_connection`` starts from a clean slate.
        """
        tc = self._connectors.get(telegram_id)
        if tc is None:
            return ConnectionFailure("no_session")

        try:
            try:
                await asyncio.wait_for(
                    self._wait_until_connected(tc),
                    timeout=self._connect_timeout_seconds,
                )
            except TimeoutError:
                await self._audit.record_error(
                    source="TON Connect",
                    message=(
                        f"await_connection timeout for telegram_id={telegram_id}"
                    ),
                    target_id=telegram_id,
                )
                return ConnectionFailure("timeout")

            try:
                proof = _extract_ton_proof(tc.wallet)
            except Exception as exc:
                await self._audit.record_error(
                    source="TON Connect",
                    message=f"malformed wallet proof: {exc!r}",
                    target_id=telegram_id,
                )
                return ConnectionFailure("sdk_error")

            now = self._clock()
            try:
                address = await verify_proof(
                    proof, telegram_id, self._redis, now=now
                )
            except InvalidProof as exc:
                await self._audit.record_error(
                    source="TON Connect",
                    message=(
                        f"proof verification failed for telegram_id="
                        f"{telegram_id}: {exc.reason}"
                    ),
                    target_id=telegram_id,
                )
                return ConnectionFailure(exc.reason)

            wallet_name = _safe_wallet_name(tc.wallet)
            await self._users_repo.set_wallet(
                telegram_id, address, wallet_name, now
            )
            await self._audit.record_info(
                event="ton_connect_ok",
                details={"user_id": telegram_id},
                now=now,
            )
            return ConnectionSuccess(
                address=address,
                wallet_name=wallet_name,
                connected_at=now,
            )
        except Exception as exc:
            await self._audit.record_error(
                source="TON Connect",
                message=f"sdk error for telegram_id={telegram_id}: {exc!r}",
                target_id=telegram_id,
            )
            return ConnectionFailure("sdk_error")
        finally:
            self._connectors.pop(telegram_id, None)

    @staticmethod
    async def _wait_until_connected(tc: Any) -> None:
        """Poll ``tc.connected`` every second until the wallet approves.

        ``asyncio.sleep`` is cancellable so the outer ``asyncio.wait_for``
        stops this coroutine as soon as its budget expires.
        """
        while not tc.connected:
            await asyncio.sleep(1)

    # ------------------------------------------------------------------
    # disconnect
    # ------------------------------------------------------------------
    async def disconnect(self, telegram_id: int) -> None:
        """Tear down the TON Connect session and clear the wallet binding.

        Requirement 3.4. Idempotent: calling this for a user with no
        active session or no persisted wallet is a no-op.
        """
        tc = self._connectors.pop(telegram_id, None)
        if tc is not None:
            try:
                await tc.disconnect()
            except Exception as exc:
                log.warning(
                    "tc_disconnect_error",
                    telegram_id=telegram_id,
                    error=repr(exc),
                )
        else:
            # No in-process connector — the user may have been reloaded
            # between /connect_wallet and /disconnect_wallet. Try to restore
            # from Redis so the remote bridge learns about the disconnect.
            try:
                from pytonconnect import TonConnect
            except ImportError:
                log.warning("tc_sdk_missing_on_disconnect", telegram_id=telegram_id)
            else:
                storage = RedisSessionStore(
                    self._redis, telegram_id, ttl=self._session_ttl_seconds
                )
                fresh = TonConnect(str(self._settings.ton_manifest_url), storage)
                try:
                    await fresh.restore_connection()
                    await fresh.disconnect()
                except Exception as exc:
                    log.debug(
                        "tc_restore_disconnect_failed",
                        telegram_id=telegram_id,
                        error=repr(exc),
                    )

        await self._users_repo.clear_wallet(telegram_id)
        await self._purge_redis_keys(telegram_id)

    # ------------------------------------------------------------------
    # send_transaction (task 26.1 — TON payments)
    # ------------------------------------------------------------------
    async def send_transaction(
        self,
        telegram_id: int,
        *,
        to: str,
        amount_nano: int,
        payload: str,
        valid_until: int | None = None,
    ) -> Any:
        """Ask the connected wallet to sign a TON transfer.

        Thin wrapper over :meth:`pytonconnect.TonConnect.send_transaction`
        kept here so the payments module does not have to know about the
        SDK or the in-process connector cache. Used by
        :class:`app.features.payments.ton.TonPaymentsService` to kick off a
        payment (Req 13.2) — the on-chain confirmation is found later by
        the polling job (task 26.2) via :class:`TonApiClient`.

        ``payload`` is a plain text comment (the bot-side ``payload_id``
        UUID). It is encoded into a base64 BoC ``text_comment`` cell
        (op = 0x00000000 + UTF-8 body) so that the TonCenter / TonAPI
        transaction payload lookup can match it.

        Args:
            telegram_id: Owner of the currently live TON Connect session.
                The user must have completed :meth:`start_connection` /
                :meth:`await_connection` earlier; otherwise we try to
                restore the session from Redis.
            to: Destination address (user-friendly or raw) — typically
                ``settings.ton_receive_address``.
            amount_nano: Transfer value in nanoTON (1 TON = 1e9 nanoTON).
            payload: Plain text comment to embed. Encoded as the standard
                ``text_comment`` cell per TON transfer conventions.
            valid_until: UNIX timestamp past which the wallet should drop
                the signing request. Defaults to ``now + 600`` seconds.

        Returns:
            Whatever the SDK returns on success (typically a ``dict``
            containing the signed transaction BoC). Callers that want a
            deeplink/QR can inspect the result; otherwise discard.

        Raises:
            RuntimeError: When the user has no active TON Connect session
                and it cannot be restored, or when the required SDKs are
                not installed.
            ValueError: When ``amount_nano < 1``.
        """
        if amount_nano < 1:
            raise ValueError("amount_nano must be >= 1")

        # Resolve a usable pytonconnect.TonConnect — prefer the in-process
        # cache, fall back to a Redis-restored session so a handler that
        # crosses process boundaries still works.
        tc = self._connectors.get(telegram_id)
        restored = False
        if tc is None:
            try:
                from pytonconnect import TonConnect
            except ImportError as exc:
                raise RuntimeError(
                    "pytonconnect is not installed; TON payments disabled"
                ) from exc
            storage = RedisSessionStore(
                self._redis, telegram_id, ttl=self._session_ttl_seconds
            )
            tc = TonConnect(str(self._settings.ton_manifest_url), storage)
            try:
                await tc.restore_connection()
            except Exception as exc:
                raise RuntimeError(
                    f"no active TON Connect session for telegram_id={telegram_id}"
                ) from exc
            if not getattr(tc, "connected", False):
                raise RuntimeError(
                    f"no active TON Connect session for telegram_id={telegram_id}"
                )
            restored = True

        now = self._clock()
        if valid_until is None:
            valid_until = int(now.timestamp()) + 600

        payload_b64 = _encode_text_comment(payload)

        request = {
            "valid_until": int(valid_until),
            "messages": [
                {
                    "address": str(to),
                    "amount": str(int(amount_nano)),
                    "payload": payload_b64,
                }
            ],
        }

        try:
            return await tc.send_transaction(request)
        finally:
            if restored:
                # Do not leak the transient connector — the handler will
                # rebuild it next time. Keep cached connectors untouched
                # so the live await_connection flow keeps working.
                pass

    async def _purge_redis_keys(self, telegram_id: int) -> None:
        """Wipe every ``tc:session:{id}:*`` key and the ``tc:nonce:{id}``."""
        pattern = f"{_SESSION_KEY_PREFIX}:{telegram_id}:*"
        async for key in self._redis.scan_iter(match=pattern):
            await self._redis.delete(key)
        await self._redis.delete(f"{_NONCE_KEY_PREFIX}:{telegram_id}")
        await self._redis.delete(self._meta_key(telegram_id))

    # ------------------------------------------------------------------
    # Connect-meta helpers (task 15.2)
    # ------------------------------------------------------------------
    @staticmethod
    def _meta_key(telegram_id: int) -> str:
        """Redis key for the per-user ``/connect_wallet`` message metadata."""
        return f"{_CONNECT_META_KEY_PREFIX}:{telegram_id}"

    async def save_connect_meta(
        self,
        telegram_id: int,
        chat_id: int,
        message_id: int,
        kind: str,
        expires_at: datetime,
    ) -> None:
        """Persist chat/message metadata for the outbound deeplink message.

        Requirement 3.5. Written by ``/connect_wallet`` right after the
        outbound message is sent; read by ``ton_session_cleanup`` (task 15.2)
        to edit that message once the session expires. TTL is
        ``session_ttl_seconds + 120`` so the cleanup job can still look it up
        after the ``tc:session:{id}:*`` keys have been evicted.
        """
        if kind not in ("photo", "text"):
            raise ValueError(f"kind must be 'photo' or 'text', got {kind!r}")
        key = self._meta_key(telegram_id)
        mapping: dict[str, str] = {
            "chat_id": str(int(chat_id)),
            "message_id": str(int(message_id)),
            "kind": kind,
            "expires_at": expires_at.isoformat(),
        }
        await self._redis.hset(key, mapping=mapping)
        await self._redis.expire(
            key, self._session_ttl_seconds + _CONNECT_META_GRACE_SECONDS
        )

    async def clear_connect_meta(self, telegram_id: int) -> None:
        """Delete the per-user ``tc:connect_meta`` hash.

        Called from ``/disconnect_wallet`` and after a terminal
        ``ConnectionSuccess``/``ConnectionFailure`` so the cleanup job
        does not double-notify the user (Requirement 3.5).
        """
        await self._redis.delete(self._meta_key(telegram_id))

    async def iter_connect_meta(self) -> AsyncIterator[ConnectMeta]:
        """Iterate over every live ``tc:connect_meta:*`` record.

        Used by ``ton_session_cleanup`` to find outbound deeplink messages
        that may need to be edited after their session TTL (Requirement 3.5).
        """
        async for raw_key in self._redis.scan_iter(
            match=f"{_CONNECT_META_KEY_PREFIX}:*"
        ):
            key = _decode(raw_key)
            parts = key.split(":")
            if len(parts) < 3:
                continue
            try:
                telegram_id = int(parts[-1])
            except ValueError:
                continue
            data = await self._redis.hgetall(key)
            if not data:
                continue
            decoded = {_decode(k): _decode(v) for k, v in data.items()}
            try:
                chat_id = int(decoded["chat_id"])
                message_id = int(decoded["message_id"])
                kind = decoded["kind"]
                expires_at = datetime.fromisoformat(decoded["expires_at"])
            except (KeyError, ValueError) as exc:
                log.warning(
                    "tc_connect_meta_malformed",
                    telegram_id=telegram_id,
                    error=repr(exc),
                )
                continue
            yield ConnectMeta(
                telegram_id=telegram_id,
                chat_id=chat_id,
                message_id=message_id,
                kind=kind,
                expires_at=expires_at,
            )

    async def has_session_keys(self, telegram_id: int) -> bool:
        """Return True while any ``tc:session:{id}:*`` key still exists.

        ``tc_session_cleanup`` treats a user as timed out once this flips to
        False — Redis TTL is the authoritative enforcement of Requirement 3.5.
        """
        pattern = f"{_SESSION_KEY_PREFIX}:{telegram_id}:*"
        async for _ in self._redis.scan_iter(match=pattern):
            return True
        return False

    # ------------------------------------------------------------------
    # Scheduler helper
    # ------------------------------------------------------------------
    async def list_active_sessions(self) -> list[int]:
        """Return the sorted distinct telegram ids with any live session key.

        Used by the ``tc_session_cleanup`` APScheduler job (task 15.2) to
        find users whose TON Connect session has expired since Redis TTL
        does the enforcement; this only enumerates current holders.
        """
        found: set[int] = set()
        async for raw_key in self._redis.scan_iter(
            match=f"{_SESSION_KEY_PREFIX}:*:*"
        ):
            key = raw_key.decode("utf-8") if isinstance(raw_key, bytes) else raw_key
            parts = key.split(":")
            if len(parts) < 3:
                continue
            try:
                found.add(int(parts[2]))
            except ValueError:
                continue
        return sorted(found)


# ---------------------------------------------------------------------------
# Helpers — kept module-level so they are easy to unit-test.
# ---------------------------------------------------------------------------


def _encode_text_comment(text: str) -> str:
    """Encode ``text`` as a TON ``text_comment`` cell and return base64.

    Implements the standard on-chain comment format used by TonKeeper and
    every major TON wallet: a cell whose data is the 4-byte big-endian
    op-code ``0x00000000`` followed by the UTF-8 bytes of ``text``. The
    cell is serialized as a BoC (BagOfCells) and base64-encoded — that's
    the format ``pytonconnect.send_transaction`` expects in
    ``messages[].payload``.

    Uses ``pytoniq_core`` (already pinned in ``pyproject.toml``) when
    available — its ``Cell``/``begin_cell`` / ``to_boc()`` is battle-tested
    across every TON client. Falls back to a minimal in-tree BoC encoder
    otherwise so unit tests and environments without the native build of
    pytoniq still load the module.
    """
    body = text.encode("utf-8")
    if len(body) > 123:
        raise ValueError(
            f"text comment too long: {len(body)} bytes > 123 byte cell budget"
        )

    # Preferred path — use pytoniq_core if installed.
    try:
        from pytoniq_core import begin_cell  # type: ignore[import-not-found]
    except ImportError:
        pass
    else:
        import base64 as _b64

        cell = (
            begin_cell()
            .store_uint(0, 32)  # text_comment op-code
            .store_snake_string(text)
            .end_cell()
        )
        return _b64.b64encode(cell.to_boc()).decode("ascii")

    # Fallback path — hand-rolled minimal BoC. Only exercised when
    # pytoniq_core is not installed (tests, lightweight CI).
    import base64
    import zlib

    data = b"\x00\x00\x00\x00" + body
    d1 = 0  # 0 refs, ordinary cell
    d2 = len(data) * 2  # byte-aligned → floor == ceil
    cell_bytes = bytes([d1, d2]) + data

    off_bytes = 1
    ref_size = 1
    header = bytes.fromhex("B5EE9C72")
    header += bytes([0b01000001])  # has_crc32c=1, ref_size=1
    header += bytes([off_bytes])
    header += (1).to_bytes(ref_size, "big")  # cells
    header += (1).to_bytes(ref_size, "big")  # roots
    header += (0).to_bytes(ref_size, "big")  # absent
    header += len(cell_bytes).to_bytes(off_bytes, "big")
    header += (0).to_bytes(ref_size, "big")  # root idx

    body_bytes = header + cell_bytes
    crc = zlib.crc32(body_bytes) & 0xFFFFFFFF
    return base64.b64encode(body_bytes + crc.to_bytes(4, "little")).decode(
        "ascii"
    )


def _decode(value: Any) -> str:
    """Decode bytes → str while leaving ``str`` inputs untouched.

    ``redis.asyncio.Redis`` can be configured either with ``decode_responses``
    on (returns ``str``) or off (returns ``bytes``); callers should not have
    to care about that distinction.
    """
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


def _extract_ton_proof(wallet: Any) -> TonProof:
    """Translate ``pytonconnect`` wallet reply into our :class:`TonProof`.

    The SDK exposes:

    * ``wallet.connect_items.ton_proof`` — holds ``payload`` (str),
      ``signature`` (base64 str), ``timestamp`` (int) and ``domain.value``
      (the host string the wallet actually signed).
    * ``wallet.account.public_key`` — hex ed25519 public key.
    * ``wallet.account.address`` — raw ``<workchain>:<hex>`` address;
      we decompose it via :class:`pytoniq_core.Address`.

    Any missing/invalid field raises — the caller records an audit error
    and returns ``ConnectionFailure("sdk_error")``.
    """
    raw = wallet.connect_items.ton_proof
    addr = Address(wallet.account.address)
    signature_raw = raw.signature
    if isinstance(signature_raw, str):
        signature_bytes = base64.b64decode(signature_raw)
    else:
        signature_bytes = bytes(signature_raw)
    return TonProof(
        payload=str(raw.payload),
        signature=signature_bytes,
        wallet_pubkey=bytes.fromhex(wallet.account.public_key),
        address_workchain=int(addr.wc),
        address_hash=bytes(addr.hash_part),
        domain=str(raw.domain.value),
        timestamp=int(raw.timestamp),
    )


def _safe_wallet_name(wallet: Any) -> str | None:
    """Return ``wallet.device.app_name`` if available, else ``None``."""
    device = getattr(wallet, "device", None)
    if device is None:
        return None
    name = getattr(device, "app_name", None)
    return str(name) if name else None


__all__ = [
    "AlreadyConnectedError",
    "ConnectMeta",
    "ConnectionFailure",
    "ConnectionResult",
    "ConnectionSuccess",
    "StartResult",
    "TonConnector",
    "_encode_text_comment",
]
