"""TON Connect 2 ``ton_proof`` verifier.

Validates a wallet-produced ``ton_proof`` before :class:`TonConnector.await_connection`
(task 14.3) persists ``ton_address`` on the user. Consumers call
:func:`verify_proof`; it returns the user-friendly (bounceable, url-safe) TON
address on success or raises :class:`InvalidProof` with a human-readable
``reason`` on any failure — the caller then surfaces the reason to the user and
records ``audit.record_error(source="TON Connect", ...)``.

Requirements:
- 3.2 — on wallet approve the TON_Connector SHALL verify the signature (``proof``)
  and store ``ton_address``, ``ton_wallet_name``, ``ton_connected_at``.
- 3.3 — if verification fails, the connection SHALL be rejected, no address
  persisted and an error returned to the user.

Payload format (design.md §TON Connect / "Proof payload"):

    tg:<telegram_id>:<issued_at>:<random_nonce>

Validation order (fail-fast; the first failing step raises :class:`InvalidProof`):

1. Parse ``payload`` as ``tg:<id>:<ts>:<nonce>``.
2. Parsed ``<id>`` must equal ``telegram_id``.
3. ``<ts>`` must lie in ``[now - 600 s, now + 60 s]`` (10-minute past window,
   1-minute future clock skew).
4. ``GETDEL tc:nonce:{telegram_id}`` must return the same nonce — atomic
   consume so a replay of the same proof fails.
5. Ed25519 signature over the TON Connect 2 ``ton-proof-item-v2`` message is
   verified against ``wallet_pubkey``.

Signing schema (TON Connect 2 ``ton_proof``)::

    inner = "ton-proof-item-v2/"
          + workchain        (4B BE signed int)
          + address_hash     (32B)
          + domain_len       (4B LE uint)
          + domain           (utf-8 bytes, domain_len bytes)
          + timestamp        (8B LE int)
          + payload_bytes    (utf-8 bytes of `payload`)

    signed = 0xffff || "ton-connect" || sha256(inner)
    signature = ed25519_sign(wallet_priv, sha256(signed))

``_build_message_to_sign`` returns ``inner``; ``_compute_signing_digest``
returns the final 32-byte digest ``sha256(signed)`` that is passed to
``nacl.signing.VerifyKey.verify``. Both helpers are kept private but stable so
the unit test in task 14.2 can feed known TON Connect vectors.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey
from pytoniq_core import Address

from app.core.utils.clock import utc_now

if TYPE_CHECKING:  # pragma: no cover - typing only
    from redis.asyncio import Redis


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


class InvalidProof(Exception):
    """Raised when a TON Connect ``ton_proof`` fails verification.

    ``reason`` is a short machine-friendly tag (``"payload_shape"``,
    ``"telegram_id_mismatch"``, ``"timestamp_out_of_window"``, ``"nonce"``,
    ``"signature"``, ...). It is safe to surface in logs; handlers map it to a
    localized user message.
    """

    def __init__(self, reason: str, *, detail: str | None = None) -> None:
        self.reason = reason
        self.detail = detail
        message = reason if detail is None else f"{reason}: {detail}"
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class TonProof:
    """Subset of the TON Connect 2 ``ton_proof`` response the verifier needs.

    The SDK wrapper (``TonConnector.await_connection``) is responsible for
    assembling this from the wallet's reply. Keeping the dataclass minimal and
    explicit avoids leaking ``pytonconnect`` types into the verifier.

    Attributes:
        payload:         Original ``tg:<id>:<ts>:<nonce>`` payload string the
                         bot supplied in the ``ton_proof`` connect request.
        signature:       64-byte ed25519 signature produced by the wallet.
        wallet_pubkey:   32-byte ed25519 public key exposed by the wallet.
        address_workchain: TON workchain id (typically ``0`` for basechain,
                         ``-1`` for masterchain).
        address_hash:    32-byte account hash part of the wallet address.
        domain:          DApp domain exactly as the wallet signed it
                         (host component, no scheme/path).
        timestamp:       Wallet-reported unix timestamp (seconds) at which the
                         proof was produced. NOTE: this is *not* the ``<ts>``
                         inside ``payload``; it is signed into the TON Connect
                         message separately per spec.
    """

    payload: str
    signature: bytes
    wallet_pubkey: bytes
    address_workchain: int
    address_hash: bytes
    domain: str
    timestamp: int


_NONCE_KEY_PREFIX = "tc:nonce"
_PAYLOAD_PREFIX = "tg"
_PAST_WINDOW_SECONDS = 600  # 10 minutes
_FUTURE_WINDOW_SECONDS = 60  # 1 minute clock-skew tolerance


async def verify_proof(
    proof: TonProof,
    telegram_id: int,
    redis: Redis,
    *,
    now: datetime | None = None,
) -> str:
    """Verify a TON Connect ``ton_proof`` and return the user-friendly address.

    Args:
        proof:        Parsed proof fields from the wallet.
        telegram_id:  Telegram user the proof is expected to bind to.
        redis:        Async Redis client used to atomically consume the nonce
                      under ``tc:nonce:{telegram_id}``.
        now:          Optional clock override (UTC datetime). When ``None`` the
                      module's :func:`~app.core.utils.clock.utc_now` is used;
                      tests inject a frozen clock.

    Returns:
        The wallet's TON address in user-friendly, url-safe, bounceable form
        (``EQ...``). This value is what :class:`TonConnector` persists in
        ``users.ton_address`` (Requirement 3.2).

    Raises:
        InvalidProof: Any step of the validation failed. The ``reason``
                      attribute identifies the failed step. On this path no
                      state is mutated beyond the atomic nonce consume that
                      may have already happened — replays therefore still
                      fail on the nonce step.
    """
    # 1) Payload shape.
    parsed_id, parsed_ts, parsed_nonce = _parse_payload(proof.payload)

    # 2) Telegram id binding.
    if parsed_id != telegram_id:
        raise InvalidProof("telegram_id_mismatch")

    # 3) Timestamp window.
    current = now if now is not None else utc_now()
    current_ts = int(current.timestamp())
    if not (
        current_ts - _PAST_WINDOW_SECONDS
        <= parsed_ts
        <= current_ts + _FUTURE_WINDOW_SECONDS
    ):
        raise InvalidProof("timestamp_out_of_window")

    # 4) Atomic nonce consume. GETDEL requires Redis >= 6.2 (our default is 7).
    nonce_key = f"{_NONCE_KEY_PREFIX}:{telegram_id}"
    stored_nonce = await redis.getdel(nonce_key)
    if stored_nonce is None or stored_nonce != parsed_nonce:
        raise InvalidProof("nonce")

    # 5) Ed25519 signature over the TON Connect 2 ton_proof schema.
    if len(proof.wallet_pubkey) != 32:
        raise InvalidProof("wallet_pubkey_length")
    if len(proof.signature) != 64:
        raise InvalidProof("signature_length")
    if len(proof.address_hash) != 32:
        raise InvalidProof("address_hash_length")

    digest = _compute_signing_digest(
        workchain=proof.address_workchain,
        address_hash=proof.address_hash,
        domain=proof.domain,
        timestamp=proof.timestamp,
        payload=proof.payload,
    )
    try:
        VerifyKey(proof.wallet_pubkey).verify(digest, proof.signature)
    except BadSignatureError as err:
        raise InvalidProof("signature") from err

    # Success — return the canonical user-friendly bounceable address.
    address = Address((proof.address_workchain, proof.address_hash))
    return address.to_str(is_user_friendly=True, is_url_safe=True, is_bounceable=True)


# ---------------------------------------------------------------------------
# Internals (exposed for unit tests — task 14.2)
# ---------------------------------------------------------------------------


def _parse_payload(payload: str) -> tuple[int, int, str]:
    """Parse ``tg:<id>:<ts>:<nonce>`` into its three components.

    Raises :class:`InvalidProof` with ``reason="payload_shape"`` on any
    structural problem (wrong prefix, wrong part count, non-integer id/ts,
    empty nonce). Accepts positive ids and timestamps only.
    """
    parts = payload.split(":")
    if len(parts) != 4:
        raise InvalidProof("payload_shape", detail="expected 4 colon-separated parts")
    prefix, id_str, ts_str, nonce = parts
    if prefix != _PAYLOAD_PREFIX:
        raise InvalidProof("payload_shape", detail="prefix must be 'tg'")
    if not nonce:
        raise InvalidProof("payload_shape", detail="empty nonce")
    try:
        parsed_id = int(id_str)
        parsed_ts = int(ts_str)
    except ValueError as err:
        raise InvalidProof("payload_shape", detail="non-integer id or ts") from err
    if parsed_id <= 0 or parsed_ts <= 0:
        raise InvalidProof("payload_shape", detail="non-positive id or ts")
    return parsed_id, parsed_ts, nonce


def _build_message_to_sign(
    *,
    workchain: int,
    address_hash: bytes,
    domain: str,
    timestamp: int,
    payload: str,
) -> bytes:
    """Assemble the inner ``ton-proof-item-v2`` message bytes.

    Per TON Connect 2 spec::

        "ton-proof-item-v2/"
        || workchain        (4B BE, signed)
        || address_hash     (32B)
        || domain_len       (4B LE, uint32)
        || domain           (utf-8)
        || timestamp        (8B LE, int64)
        || payload_bytes    (utf-8)

    This is the string fed into the inner ``sha256`` of the signing schema;
    see :func:`_compute_signing_digest`. Exposed at module level so the unit
    test in task 14.2 can drive it with known vectors.
    """
    domain_bytes = domain.encode("utf-8")
    payload_bytes = payload.encode("utf-8")
    return b"".join(
        (
            b"ton-proof-item-v2/",
            struct.pack(">i", workchain),
            address_hash,
            struct.pack("<I", len(domain_bytes)),
            domain_bytes,
            struct.pack("<q", timestamp),
            payload_bytes,
        )
    )


def _compute_signing_digest(
    *,
    workchain: int,
    address_hash: bytes,
    domain: str,
    timestamp: int,
    payload: str,
) -> bytes:
    """Compute the 32-byte digest the wallet signed.

    ``sha256(0xffff || "ton-connect" || sha256(inner_message))`` — this is the
    exact hash fed to ed25519 verify per TON Connect 2. Kept private/testable.
    """
    inner = _build_message_to_sign(
        workchain=workchain,
        address_hash=address_hash,
        domain=domain,
        timestamp=timestamp,
        payload=payload,
    )
    inner_hash = hashlib.sha256(inner).digest()
    outer = b"\xff\xff" + b"ton-connect" + inner_hash
    return hashlib.sha256(outer).digest()


__all__ = [
    "InvalidProof",
    "TonProof",
    "verify_proof",
]
