"""Unit tests for :mod:`app.ton.verifier` (task 14.2).

Covers Requirement 3.3: a failing ``ton_proof`` MUST be rejected with
:class:`InvalidProof` and never return a TON address. Each test pins the
machine-friendly ``reason`` tag produced by the verifier so a regression in
the validation order or wording fails loudly.

Signing vectors are generated in-process with :class:`nacl.signing.SigningKey`
over the digest returned by :func:`app.ton.verifier._compute_signing_digest`,
which is exactly what the wallet signs per TON Connect 2. No network, no
disk, no testcontainers: this is a pure unit test.
"""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from nacl.signing import SigningKey

from app.ton.verifier import (
    InvalidProof,
    TonProof,
    _compute_signing_digest,
    verify_proof,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


class FakeRedis:
    """Minimal in-memory async Redis stub supporting only what the verifier uses."""

    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    async def set(self, key: str, value: str, *, ex: int | None = None) -> None:
        self._data[key] = value

    async def get(self, key: str) -> str | None:
        return self._data.get(key)

    async def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            if key in self._data:
                del self._data[key]
                removed += 1
        return removed

    async def getdel(self, key: str) -> str | None:
        return self._data.pop(key, None)


@pytest.fixture
def fake_redis() -> FakeRedis:
    return FakeRedis()


@pytest.fixture
def signing_key() -> SigningKey:
    # Deterministic but still a real ed25519 key — the seed bytes are arbitrary.
    return SigningKey(b"\x11" * 32)


@pytest.fixture
def now() -> datetime:
    return datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC)


async def _prepare(fake_redis: FakeRedis, telegram_id: int, nonce: str) -> None:
    fake_redis._data[f"tc:nonce:{telegram_id}"] = nonce


def _make_proof(
    *,
    signing_key: SigningKey,
    payload: str,
    workchain: int = 0,
    address_hash: bytes | None = None,
    domain: str = "example.com",
    timestamp: int = 1_700_000_000,
    signature_override: bytes | None = None,
    pubkey_override: bytes | None = None,
    address_hash_override: bytes | None = None,
) -> TonProof:
    """Build a valid TonProof (real ed25519 signature) or allow overrides."""
    ahash = address_hash if address_hash is not None else b"\xAB" * 32
    digest = _compute_signing_digest(
        workchain=workchain,
        address_hash=ahash,
        domain=domain,
        timestamp=timestamp,
        payload=payload,
    )
    signature = signing_key.sign(digest).signature
    return TonProof(
        payload=payload,
        signature=signature_override if signature_override is not None else signature,
        wallet_pubkey=(
            pubkey_override
            if pubkey_override is not None
            else bytes(signing_key.verify_key)
        ),
        address_workchain=workchain,
        address_hash=address_hash_override if address_hash_override is not None else ahash,
        domain=domain,
        timestamp=timestamp,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_valid_proof_returns_user_friendly_address(
    fake_redis: FakeRedis, signing_key: SigningKey, now: datetime
) -> None:
    telegram_id = 42
    nonce = "not-used"
    payload = f"tg:{telegram_id}:{int(now.timestamp())}:{nonce}"
    address_hash = os.urandom(32)
    proof = _make_proof(
        signing_key=signing_key,
        payload=payload,
        workchain=0,
        address_hash=address_hash,
        domain="example.com",
        timestamp=int(now.timestamp()) - 5,
    )
    await _prepare(fake_redis, telegram_id, nonce)

    address = await verify_proof(proof, telegram_id, fake_redis, now=now)  # type: ignore[arg-type]

    assert isinstance(address, str), f"expected str, got {type(address)!r}"
    assert address.startswith("EQ"), f"expected user-friendly bounceable 'EQ...', got {address!r}"
    assert len(address) == 48, f"expected 48-char address, got {len(address)}: {address!r}"


async def test_invalid_signature_raises(
    fake_redis: FakeRedis, signing_key: SigningKey, now: datetime
) -> None:
    telegram_id = 42
    nonce = "nonce-sig"
    payload = f"tg:{telegram_id}:{int(now.timestamp())}:{nonce}"
    proof = _make_proof(signing_key=signing_key, payload=payload)
    # Flip one byte of the signature — still 64 bytes, but mathematically invalid.
    tampered = bytearray(proof.signature)
    tampered[0] ^= 0x01
    broken = replace(proof, signature=bytes(tampered))
    await _prepare(fake_redis, telegram_id, nonce)

    with pytest.raises(InvalidProof) as err:
        await verify_proof(broken, telegram_id, fake_redis, now=now)  # type: ignore[arg-type]
    assert err.value.reason == "signature", f"got reason={err.value.reason!r}"


async def test_expired_timestamp_raises(
    fake_redis: FakeRedis, signing_key: SigningKey, now: datetime
) -> None:
    telegram_id = 42
    nonce = "nonce-expired"
    payload = f"tg:{telegram_id}:{int(now.timestamp()) - 3600}:{nonce}"
    proof = _make_proof(signing_key=signing_key, payload=payload)
    await _prepare(fake_redis, telegram_id, nonce)

    with pytest.raises(InvalidProof) as err:
        await verify_proof(proof, telegram_id, fake_redis, now=now)  # type: ignore[arg-type]
    assert err.value.reason == "timestamp_out_of_window", f"got reason={err.value.reason!r}"


async def test_future_timestamp_raises(
    fake_redis: FakeRedis, signing_key: SigningKey, now: datetime
) -> None:
    telegram_id = 42
    nonce = "nonce-future"
    payload = f"tg:{telegram_id}:{int(now.timestamp()) + 3600}:{nonce}"
    proof = _make_proof(signing_key=signing_key, payload=payload)
    await _prepare(fake_redis, telegram_id, nonce)

    with pytest.raises(InvalidProof) as err:
        await verify_proof(proof, telegram_id, fake_redis, now=now)  # type: ignore[arg-type]
    assert err.value.reason == "timestamp_out_of_window", f"got reason={err.value.reason!r}"


async def test_telegram_id_mismatch_raises(
    fake_redis: FakeRedis, signing_key: SigningKey, now: datetime
) -> None:
    payload = f"tg:100:{int(now.timestamp())}:nonce-mismatch"
    proof = _make_proof(signing_key=signing_key, payload=payload)
    # No nonce seeded — telegram_id check runs before the nonce step anyway.

    with pytest.raises(InvalidProof) as err:
        await verify_proof(proof, telegram_id=42, redis=fake_redis, now=now)  # type: ignore[arg-type]
    assert err.value.reason == "telegram_id_mismatch", f"got reason={err.value.reason!r}"


async def test_nonce_missing_raises(
    fake_redis: FakeRedis, signing_key: SigningKey, now: datetime
) -> None:
    telegram_id = 42
    payload = f"tg:{telegram_id}:{int(now.timestamp())}:not-seeded"
    proof = _make_proof(signing_key=signing_key, payload=payload)
    # Intentionally do NOT seed tc:nonce:42.

    with pytest.raises(InvalidProof) as err:
        await verify_proof(proof, telegram_id, fake_redis, now=now)  # type: ignore[arg-type]
    assert err.value.reason == "nonce", f"got reason={err.value.reason!r}"


async def test_nonce_replay_raises(
    fake_redis: FakeRedis, signing_key: SigningKey, now: datetime
) -> None:
    telegram_id = 42
    nonce = "nonce-replay"
    payload = f"tg:{telegram_id}:{int(now.timestamp())}:{nonce}"
    proof = _make_proof(signing_key=signing_key, payload=payload)
    await _prepare(fake_redis, telegram_id, nonce)

    # First call consumes the nonce atomically via GETDEL.
    address = await verify_proof(proof, telegram_id, fake_redis, now=now)  # type: ignore[arg-type]
    assert address.startswith("EQ")

    # Second call with the same proof must be rejected on the nonce step.
    with pytest.raises(InvalidProof) as err:
        await verify_proof(proof, telegram_id, fake_redis, now=now)  # type: ignore[arg-type]
    assert err.value.reason == "nonce", f"got reason={err.value.reason!r}"


@pytest.mark.parametrize(
    "payload",
    [
        "tg:42:123",
        "ton:42:123:abc",
        "tg:42:abc:nonce",
        "",
    ],
)
async def test_malformed_payload_shape_raises(
    fake_redis: FakeRedis, signing_key: SigningKey, now: datetime, payload: str
) -> None:
    proof = _make_proof(signing_key=signing_key, payload=payload or "tg:42:1:n")
    # Override payload on the already-built proof so the signature is bogus,
    # but payload_shape is checked first — we never reach signature verify.
    proof = replace(proof, payload=payload)

    with pytest.raises(InvalidProof) as err:
        await verify_proof(proof, telegram_id=42, redis=fake_redis, now=now)  # type: ignore[arg-type]
    assert err.value.reason == "payload_shape", (
        f"payload={payload!r} got reason={err.value.reason!r}"
    )


async def test_address_hash_length_raises(
    fake_redis: FakeRedis, signing_key: SigningKey, now: datetime
) -> None:
    telegram_id = 42
    nonce = "nonce-ahash"
    payload = f"tg:{telegram_id}:{int(now.timestamp())}:{nonce}"
    proof = _make_proof(
        signing_key=signing_key,
        payload=payload,
        address_hash_override=b"\x00" * 31,
    )
    await _prepare(fake_redis, telegram_id, nonce)

    with pytest.raises(InvalidProof) as err:
        await verify_proof(proof, telegram_id, fake_redis, now=now)  # type: ignore[arg-type]
    assert err.value.reason == "address_hash_length", f"got reason={err.value.reason!r}"


async def test_wallet_pubkey_length_raises(
    fake_redis: FakeRedis, signing_key: SigningKey, now: datetime
) -> None:
    telegram_id = 42
    nonce = "nonce-pubkey"
    payload = f"tg:{telegram_id}:{int(now.timestamp())}:{nonce}"
    proof = _make_proof(
        signing_key=signing_key,
        payload=payload,
        pubkey_override=b"\x00" * 31,
    )
    await _prepare(fake_redis, telegram_id, nonce)

    with pytest.raises(InvalidProof) as err:
        await verify_proof(proof, telegram_id, fake_redis, now=now)  # type: ignore[arg-type]
    assert err.value.reason == "wallet_pubkey_length", f"got reason={err.value.reason!r}"


async def test_signature_length_raises(
    fake_redis: FakeRedis, signing_key: SigningKey, now: datetime
) -> None:
    telegram_id = 42
    nonce = "nonce-siglen"
    payload = f"tg:{telegram_id}:{int(now.timestamp())}:{nonce}"
    proof = _make_proof(
        signing_key=signing_key,
        payload=payload,
        signature_override=b"\x00" * 63,
    )
    await _prepare(fake_redis, telegram_id, nonce)

    with pytest.raises(InvalidProof) as err:
        await verify_proof(proof, telegram_id, fake_redis, now=now)  # type: ignore[arg-type]
    assert err.value.reason == "signature_length", f"got reason={err.value.reason!r}"


async def test_now_kwarg_overrides_clock(
    fake_redis: FakeRedis, signing_key: SigningKey
) -> None:
    pinned = datetime(2030, 1, 1, tzinfo=UTC)
    pinned_ts = int(pinned.timestamp())
    telegram_id = 42

    # Stale payload from 2025 is outside the 10-minute past window relative to 2030.
    stale_payload = f"tg:{telegram_id}:{int(datetime(2025, 1, 1, tzinfo=UTC).timestamp())}:stale"
    stale_proof = _make_proof(signing_key=signing_key, payload=stale_payload)
    await _prepare(fake_redis, telegram_id, "stale")
    with pytest.raises(InvalidProof) as err:
        await verify_proof(stale_proof, telegram_id, fake_redis, now=pinned)  # type: ignore[arg-type]
    assert err.value.reason == "timestamp_out_of_window", (
        f"stale 2025 ts should be rejected vs pinned=2030, got reason={err.value.reason!r}"
    )

    # Fresh payload near 2030 is accepted.
    fresh_payload = f"tg:{telegram_id}:{pinned_ts - 5}:fresh"
    fresh_proof = _make_proof(
        signing_key=signing_key, payload=fresh_payload, timestamp=pinned_ts - 5
    )
    await _prepare(fake_redis, telegram_id, "fresh")
    address = await verify_proof(fresh_proof, telegram_id, fake_redis, now=pinned)  # type: ignore[arg-type]
    assert address.startswith("EQ"), f"expected EQ-prefixed address, got {address!r}"
