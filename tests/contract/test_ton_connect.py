"""Contract tests for :class:`app.ton.connector.TonConnector` (task 15.3).

These tests exercise the TON Connect connector against a stubbed
``pytonconnect.TonConnect`` SDK and a fake Redis/repositories/audit stack.
No real wallet, no real bridge traffic, no testcontainers — everything lives
in-process.

Requirements validated:

* **3.2** — on wallet approval the connector SHALL verify the signature
  (``proof``) and persist ``ton_address`` / ``ton_wallet_name`` /
  ``ton_connected_at`` exactly once.
* **3.3** — a failing ``ton_proof`` (bad signature, expired session, wrong
  telegram id) SHALL be rejected: no address persisted, an ``error`` audit
  record written, a user-facing failure reason surfaced.
* **3.5** — a connect session that is not completed within 10 minutes SHALL
  be marked expired and its Redis state released. ``disconnect`` SHALL wipe
  ``tc:nonce:*``, ``tc:session:{id}:*``, ``tc:connect_meta:{id}`` and stay
  idempotent when called twice.
"""

from __future__ import annotations

import asyncio
import base64
import fnmatch
import sys
import types
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from nacl.signing import SigningKey

from app.ton.connector import (
    AlreadyConnectedError,
    ConnectionFailure,
    ConnectionSuccess,
    StartResult,
    TonConnector,
)
from app.ton.verifier import _compute_signing_digest

pytestmark = pytest.mark.contract


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeRedis:
    """Minimal in-memory async Redis stand-in.

    Supports just enough of ``redis.asyncio.Redis`` for the connector: string
    ``set/get/getdel/delete``, hash ``hset/hgetall/expire``, and glob-matching
    ``scan_iter``. No TTL enforcement — tests freeze the clock anyway.
    """

    def __init__(self) -> None:
        self._strings: dict[str, str] = {}
        self._hashes: dict[str, dict[str, str]] = {}
        self._ttls: dict[str, int] = {}

    # -- strings ---------------------------------------------------------
    async def set(self, key: str, value: str, *, ex: int | None = None) -> None:
        self._strings[key] = value
        if ex is not None:
            self._ttls[key] = int(ex)

    async def get(self, key: str) -> str | None:
        return self._strings.get(key)

    async def getdel(self, key: str) -> str | None:
        return self._strings.pop(key, None)

    async def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            if key in self._strings:
                del self._strings[key]
                removed += 1
            if key in self._hashes:
                del self._hashes[key]
                removed += 1
            self._ttls.pop(key, None)
        return removed

    # -- hashes ----------------------------------------------------------
    async def hset(
        self,
        key: str,
        *,
        mapping: dict[str, str] | None = None,
    ) -> int:
        bucket = self._hashes.setdefault(key, {})
        payload = dict(mapping or {})
        bucket.update(payload)
        return len(payload)

    async def hgetall(self, key: str) -> dict[str, str]:
        return dict(self._hashes.get(key, {}))

    async def expire(self, key: str, seconds: int) -> bool:
        if key in self._strings or key in self._hashes:
            self._ttls[key] = int(seconds)
            return True
        return False

    # -- iteration -------------------------------------------------------
    async def scan_iter(self, *, match: str | None = None):
        """Yield every live key (string or hash) matching ``match`` (glob)."""
        keys = list(self._strings.keys()) + list(self._hashes.keys())
        for key in keys:
            if match is None or fnmatch.fnmatchcase(key, match):
                yield key

    # -- test helpers ----------------------------------------------------
    def has_string(self, key: str) -> bool:
        return key in self._strings

    def has_hash(self, key: str) -> bool:
        return key in self._hashes

    def glob(self, pattern: str) -> list[str]:
        keys = list(self._strings.keys()) + list(self._hashes.keys())
        return [k for k in keys if fnmatch.fnmatchcase(k, pattern)]


@dataclass
class _SetWalletCall:
    telegram_id: int
    address: str
    wallet_name: str | None
    now: datetime


@dataclass
class FakeUsersRepo:
    """Records ``UsersRepo`` invocations and serves canned ``get_by_tg_id``."""

    preexisting: dict[int, Any] = field(default_factory=dict)
    set_wallet_calls: list[_SetWalletCall] = field(default_factory=list)
    clear_wallet_calls: list[int] = field(default_factory=list)
    get_by_tg_id_calls: list[int] = field(default_factory=list)

    async def get_by_tg_id(self, telegram_id: int) -> Any | None:
        self.get_by_tg_id_calls.append(telegram_id)
        return self.preexisting.get(telegram_id)

    async def set_wallet(
        self,
        telegram_id: int,
        address: str,
        wallet_name: str | None,
        now: datetime,
    ) -> None:
        self.set_wallet_calls.append(
            _SetWalletCall(telegram_id, address, wallet_name, now)
        )

    async def clear_wallet(self, telegram_id: int) -> None:
        self.clear_wallet_calls.append(telegram_id)


@dataclass
class FakeAuditLog:
    """Records ``AuditLog.record_error`` / ``record_info`` invocations."""

    errors: list[dict[str, Any]] = field(default_factory=list)
    infos: list[dict[str, Any]] = field(default_factory=list)

    async def record_error(
        self,
        *,
        source: str,
        message: str,
        now: datetime | None = None,
        trace_id: Any | None = None,
        actor_id: int | None = None,
        target_id: int | None = None,
    ) -> None:
        self.errors.append(
            {
                "source": source,
                "message": message,
                "now": now,
                "trace_id": trace_id,
                "actor_id": actor_id,
                "target_id": target_id,
            }
        )

    async def record_info(
        self,
        *,
        event: str,
        details: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> None:
        self.infos.append({"event": event, "details": details, "now": now})


class FakeTonConnect:
    """Stand-in for ``pytonconnect.TonConnect``.

    The connector only calls a tiny subset of the SDK: ``get_wallets``,
    ``connect`` (returning a deeplink), reads ``.connected`` / ``.wallet`` in
    ``await_connection``, and invokes ``disconnect`` / ``restore_connection``
    during teardown. We record call counts so each scenario can pin them.
    """

    def __init__(self, manifest_url: str, storage: Any) -> None:
        self.manifest_url = manifest_url
        self.storage = storage
        self.connected: bool = False
        self.wallet: Any | None = None
        self.last_connect_payload: str | None = None
        self.get_wallets_calls = 0
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.restore_calls = 0
        self.deeplink = "tc://connect?v=2&id=fake-session"

    async def get_wallets(self) -> list[Any]:
        self.get_wallets_calls += 1
        return [SimpleNamespace(name="TestWallet", bridge_url="https://bridge.example/1")]

    async def connect(self, wallets: list[Any], request: dict[str, Any]) -> str:
        self.connect_calls += 1
        self.last_connect_payload = request["tonProof"]
        return self.deeplink

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self.connected = False
        self.wallet = None

    async def restore_connection(self) -> None:
        self.restore_calls += 1


class _FakeTonConnectFactory:
    """Callable factory that records every ``TonConnect(...)`` instantiation.

    Tests patch ``pytonconnect.TonConnect`` to this factory; the list of
    produced instances powers the ``already connected`` / ``disconnect``
    assertions.
    """

    def __init__(self) -> None:
        self.instances: list[FakeTonConnect] = []

    def __call__(self, manifest_url: str, storage: Any) -> FakeTonConnect:
        inst = FakeTonConnect(manifest_url, storage)
        self.instances.append(inst)
        return inst


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


TELEGRAM_ID = 42
FIXED_NOW = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)
FIXED_NOW_TS = int(FIXED_NOW.timestamp())
MANIFEST_URL = "https://example.com/tonconnect-manifest.json"
DEFAULT_DOMAIN = "bot.example.com"
DEFAULT_APP_NAME = "Tonkeeper-Test"


def _fixed_clock() -> datetime:
    return FIXED_NOW


def _make_settings() -> SimpleNamespace:
    """Minimal stub of :class:`app.config.Settings` for the connector."""
    return SimpleNamespace(ton_manifest_url=MANIFEST_URL)


def _install_fake_pytonconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> _FakeTonConnectFactory:
    """Replace the ``pytonconnect`` module with a stub exposing our factory."""
    factory = _FakeTonConnectFactory()
    module = types.ModuleType("pytonconnect")
    module.TonConnect = factory  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pytonconnect", module)
    return factory


def _build_wallet(
    *,
    signing_key: SigningKey,
    payload: str,
    address_hash: bytes = b"\xab" * 32,
    address_workchain: int = 0,
    domain: str = DEFAULT_DOMAIN,
    timestamp: int | None = None,
    app_name: str | None = DEFAULT_APP_NAME,
    tamper_signature: bool = False,
) -> SimpleNamespace:
    """Build a fake ``wallet`` object matching ``_extract_ton_proof`` shape."""
    ts = FIXED_NOW_TS - 5 if timestamp is None else timestamp
    digest = _compute_signing_digest(
        workchain=address_workchain,
        address_hash=address_hash,
        domain=domain,
        timestamp=ts,
        payload=payload,
    )
    signature = signing_key.sign(digest).signature
    if tamper_signature:
        tampered = bytearray(signature)
        tampered[0] ^= 0x01
        signature = bytes(tampered)
    return SimpleNamespace(
        connect_items=SimpleNamespace(
            ton_proof=SimpleNamespace(
                payload=payload,
                signature=base64.b64encode(signature).decode("ascii"),
                timestamp=ts,
                domain=SimpleNamespace(value=domain),
            ),
        ),
        account=SimpleNamespace(
            address=f"{address_workchain}:{address_hash.hex()}",
            public_key=bytes(signing_key.verify_key).hex(),
        ),
        device=SimpleNamespace(app_name=app_name) if app_name is not None else None,
    )


def _is_base64(value: str) -> bool:
    if not value:
        return False
    try:
        base64.b64decode(value, validate=True)
    except (ValueError, base64.binascii.Error):
        return False
    return True


@pytest.fixture
def fake_redis() -> FakeRedis:
    return FakeRedis()


@pytest.fixture
def users_repo() -> FakeUsersRepo:
    return FakeUsersRepo()


@pytest.fixture
def audit() -> FakeAuditLog:
    return FakeAuditLog()


@pytest.fixture
def signing_key() -> SigningKey:
    # Deterministic but real ed25519 key — seed is arbitrary.
    return SigningKey(b"\x17" * 32)


def _make_connector(
    fake_redis: FakeRedis,
    users_repo: FakeUsersRepo,
    audit: FakeAuditLog,
    *,
    session_ttl_seconds: int = 600,
    connect_timeout_seconds: int = 600,
) -> TonConnector:
    return TonConnector(
        fake_redis,  # type: ignore[arg-type]
        users_repo,  # type: ignore[arg-type]
        audit,  # type: ignore[arg-type]
        _make_settings(),  # type: ignore[arg-type]
        session_ttl_seconds=session_ttl_seconds,
        connect_timeout_seconds=connect_timeout_seconds,
        clock=_fixed_clock,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_happy_path_connect_proof_save(
    monkeypatch: pytest.MonkeyPatch,
    fake_redis: FakeRedis,
    users_repo: FakeUsersRepo,
    audit: FakeAuditLog,
    signing_key: SigningKey,
) -> None:
    """connect → proof → save binds the wallet exactly once (Req 3.2)."""
    factory = _install_fake_pytonconnect(monkeypatch)
    connector = _make_connector(fake_redis, users_repo, audit)

    start = await connector.start_connection(TELEGRAM_ID)

    assert isinstance(start, StartResult), f"expected StartResult, got {type(start)!r}"
    assert start.deeplink, f"expected non-empty deeplink, got {start.deeplink!r}"
    assert start.expires_at > FIXED_NOW, (
        f"expires_at must be > now={FIXED_NOW.isoformat()}, "
        f"got {start.expires_at.isoformat()}"
    )
    assert start.qr_base64 == "" or _is_base64(start.qr_base64), (
        f"qr_base64 must be empty or valid base64, got {start.qr_base64!r}"
    )
    nonce_key = f"tc:nonce:{TELEGRAM_ID}"
    assert fake_redis.has_string(nonce_key), (
        f"nonce must be stored at {nonce_key!r}; keys={list(fake_redis._strings)}"
    )
    assert len(factory.instances) == 1, (
        f"expected exactly one TonConnect instance, got {len(factory.instances)}"
    )
    fake_tc = factory.instances[0]
    assert fake_tc.get_wallets_calls == 1, (
        f"get_wallets called {fake_tc.get_wallets_calls} times, expected 1"
    )
    assert fake_tc.connect_calls == 1, (
        f"connect called {fake_tc.connect_calls} times, expected 1"
    )
    captured_payload = fake_tc.last_connect_payload
    assert captured_payload is not None, "connector must hand a payload to tc.connect"

    # Populate wallet approval on the stub with a real ed25519 signature.
    fake_tc.wallet = _build_wallet(signing_key=signing_key, payload=captured_payload)
    fake_tc.connected = True

    result = await connector.await_connection(TELEGRAM_ID)

    assert isinstance(result, ConnectionSuccess), (
        f"expected ConnectionSuccess, got {type(result).__name__}: {result!r}"
    )
    assert result.address.startswith("EQ"), (
        f"expected user-friendly bounceable 'EQ...' address, got {result.address!r}"
    )
    assert len(result.address) == 48, (
        f"expected 48-char address, got {len(result.address)}: {result.address!r}"
    )
    assert result.wallet_name == DEFAULT_APP_NAME, (
        f"expected wallet_name={DEFAULT_APP_NAME!r}, got {result.wallet_name!r}"
    )
    assert result.connected_at == FIXED_NOW, (
        f"connected_at must equal clock={FIXED_NOW.isoformat()}, "
        f"got {result.connected_at.isoformat()}"
    )

    assert len(users_repo.set_wallet_calls) == 1, (
        f"set_wallet expected 1 call, got {len(users_repo.set_wallet_calls)}"
    )
    call = users_repo.set_wallet_calls[0]
    assert call.telegram_id == TELEGRAM_ID, f"set_wallet telegram_id={call.telegram_id}"
    assert call.address == result.address, (
        f"set_wallet address={call.address!r} must equal returned {result.address!r}"
    )
    assert call.wallet_name == DEFAULT_APP_NAME, (
        f"set_wallet wallet_name={call.wallet_name!r}, expected {DEFAULT_APP_NAME!r}"
    )
    assert call.now == FIXED_NOW, (
        f"set_wallet now={call.now.isoformat()}, expected {FIXED_NOW.isoformat()}"
    )

    assert len(audit.infos) == 1, (
        f"audit.record_info expected 1 call, got {len(audit.infos)}: {audit.infos!r}"
    )
    info = audit.infos[0]
    assert info["event"] == "ton_connect_ok", (
        f"audit event={info['event']!r}, expected 'ton_connect_ok'"
    )
    assert not audit.errors, f"no audit errors expected on happy path, got {audit.errors!r}"


async def test_invalid_signature_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    fake_redis: FakeRedis,
    users_repo: FakeUsersRepo,
    audit: FakeAuditLog,
    signing_key: SigningKey,
) -> None:
    """Flipping a byte of the signature yields ConnectionFailure('signature') (Req 3.3)."""
    factory = _install_fake_pytonconnect(monkeypatch)
    connector = _make_connector(fake_redis, users_repo, audit)

    await connector.start_connection(TELEGRAM_ID)
    fake_tc = factory.instances[0]
    assert fake_tc.last_connect_payload is not None
    fake_tc.wallet = _build_wallet(
        signing_key=signing_key,
        payload=fake_tc.last_connect_payload,
        tamper_signature=True,
    )
    fake_tc.connected = True

    result = await connector.await_connection(TELEGRAM_ID)

    assert isinstance(result, ConnectionFailure), (
        f"expected ConnectionFailure, got {type(result).__name__}: {result!r}"
    )
    assert result.reason == "signature", (
        f"expected reason='signature', got {result.reason!r}"
    )
    assert not users_repo.set_wallet_calls, (
        f"set_wallet must NOT be called on bad signature; calls={users_repo.set_wallet_calls!r}"
    )
    assert len(audit.errors) == 1, (
        f"audit.record_error expected 1 call, got {len(audit.errors)}: {audit.errors!r}"
    )
    err = audit.errors[0]
    assert err["source"] == "TON Connect", (
        f"audit source={err['source']!r}, expected 'TON Connect'"
    )
    assert str(TELEGRAM_ID) in err["message"], (
        f"audit message must mention telegram_id={TELEGRAM_ID}, got {err['message']!r}"
    )
    assert not audit.infos, (
        f"no audit.record_info expected on failure path, got {audit.infos!r}"
    )


async def test_timeout_expires_session(
    monkeypatch: pytest.MonkeyPatch,
    fake_redis: FakeRedis,
    users_repo: FakeUsersRepo,
    audit: FakeAuditLog,
) -> None:
    """await_connection returns 'timeout' when the wallet never approves (Req 3.5)."""
    _install_fake_pytonconnect(monkeypatch)
    connector = _make_connector(
        fake_redis, users_repo, audit, connect_timeout_seconds=1
    )

    await connector.start_connection(TELEGRAM_ID)
    # ``tc.connected`` stays False → _wait_until_connected spins until wait_for
    # cancels it; the connector maps that to ConnectionFailure("timeout").

    result = await connector.await_connection(TELEGRAM_ID)

    assert isinstance(result, ConnectionFailure), (
        f"expected ConnectionFailure, got {type(result).__name__}: {result!r}"
    )
    assert result.reason == "timeout", (
        f"expected reason='timeout', got {result.reason!r}"
    )
    assert not users_repo.set_wallet_calls, (
        f"set_wallet must NOT be called on timeout; calls={users_repo.set_wallet_calls!r}"
    )
    assert len(audit.errors) == 1, (
        f"audit.record_error expected 1 call, got {len(audit.errors)}: {audit.errors!r}"
    )
    err = audit.errors[0]
    assert err["source"] == "TON Connect", (
        f"audit source={err['source']!r}, expected 'TON Connect'"
    )
    assert "timeout" in err["message"].lower(), (
        f"audit message must contain 'timeout', got {err['message']!r}"
    )
    assert err["target_id"] == TELEGRAM_ID, (
        f"audit target_id={err['target_id']!r}, expected {TELEGRAM_ID}"
    )


async def test_telegram_id_mismatch_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    fake_redis: FakeRedis,
    users_repo: FakeUsersRepo,
    audit: FakeAuditLog,
    signing_key: SigningKey,
) -> None:
    """Proof payload carrying another telegram id fails cleanly (Req 3.3)."""
    factory = _install_fake_pytonconnect(monkeypatch)
    connector = _make_connector(fake_redis, users_repo, audit)

    await connector.start_connection(TELEGRAM_ID)
    fake_tc = factory.instances[0]
    wrong_id = TELEGRAM_ID + 10_000
    wrong_payload = f"tg:{wrong_id}:{FIXED_NOW_TS}:unused-nonce-xyz"
    fake_tc.wallet = _build_wallet(signing_key=signing_key, payload=wrong_payload)
    fake_tc.connected = True

    result = await connector.await_connection(TELEGRAM_ID)

    assert isinstance(result, ConnectionFailure), (
        f"expected ConnectionFailure, got {type(result).__name__}: {result!r}"
    )
    assert result.reason == "telegram_id_mismatch", (
        f"expected reason='telegram_id_mismatch', got {result.reason!r}"
    )
    assert not users_repo.set_wallet_calls, (
        "set_wallet must NOT be called on telegram_id mismatch; "
        f"calls={users_repo.set_wallet_calls!r}"
    )
    assert len(audit.errors) == 1, (
        f"audit.record_error expected 1 call, got {len(audit.errors)}: {audit.errors!r}"
    )
    err = audit.errors[0]
    assert "telegram_id_mismatch" in err["message"], (
        f"audit message must mention the reason 'telegram_id_mismatch', got {err['message']!r}"
    )


async def test_already_connected_blocks_start(
    monkeypatch: pytest.MonkeyPatch,
    fake_redis: FakeRedis,
    users_repo: FakeUsersRepo,
    audit: FakeAuditLog,
) -> None:
    """If the user already bound a wallet, start_connection raises (Req 3.6)."""
    factory = _install_fake_pytonconnect(monkeypatch)
    users_repo.preexisting[TELEGRAM_ID] = SimpleNamespace(
        ton_address="EQ" + "A" * 46,
        ton_wallet_name="Existing",
    )
    connector = _make_connector(fake_redis, users_repo, audit)

    with pytest.raises(AlreadyConnectedError) as err:
        await connector.start_connection(TELEGRAM_ID)
    assert str(TELEGRAM_ID) in str(err.value), (
        f"AlreadyConnectedError message must include telegram_id={TELEGRAM_ID}, "
        f"got {str(err.value)!r}"
    )

    nonce_key = f"tc:nonce:{TELEGRAM_ID}"
    assert not fake_redis.has_string(nonce_key), (
        f"nonce must NOT be written when already connected; keys={list(fake_redis._strings)}"
    )
    assert factory.instances == [], (
        f"TonConnect must NOT be instantiated when user already has a wallet; "
        f"instantiations={len(factory.instances)}"
    )


async def test_disconnect_clears_state_and_session_keys(
    monkeypatch: pytest.MonkeyPatch,
    fake_redis: FakeRedis,
    users_repo: FakeUsersRepo,
    audit: FakeAuditLog,
    signing_key: SigningKey,
) -> None:
    """After a successful bind, ``disconnect`` wipes every tc:* key (Req 3.5)."""
    factory = _install_fake_pytonconnect(monkeypatch)
    connector = _make_connector(fake_redis, users_repo, audit)

    # Happy path: bind a wallet first.
    await connector.start_connection(TELEGRAM_ID)
    fake_tc = factory.instances[0]
    assert fake_tc.last_connect_payload is not None
    fake_tc.wallet = _build_wallet(
        signing_key=signing_key, payload=fake_tc.last_connect_payload
    )
    fake_tc.connected = True
    outcome = await connector.await_connection(TELEGRAM_ID)
    assert isinstance(outcome, ConnectionSuccess), (
        f"precondition — happy path must succeed, got {outcome!r}"
    )

    # Seed the cleanup surface: pytonconnect-style session keys + the
    # /connect_wallet message metadata the bot writes after sending the QR.
    await fake_redis.set(f"tc:session:{TELEGRAM_ID}:bridge", "payload-A")
    await fake_redis.set(f"tc:session:{TELEGRAM_ID}:state", "payload-B")
    await connector.save_connect_meta(
        TELEGRAM_ID,
        chat_id=10_001,
        message_id=777,
        kind="photo",
        expires_at=FIXED_NOW,
    )
    session_keys_before = fake_redis.glob(f"tc:session:{TELEGRAM_ID}:*")
    assert len(session_keys_before) == 2, (
        f"seeded 2 session keys but got {session_keys_before!r}"
    )
    assert fake_redis.has_hash(f"tc:connect_meta:{TELEGRAM_ID}"), (
        "precondition — connect_meta hash must be present before disconnect"
    )

    await connector.disconnect(TELEGRAM_ID)

    assert len(users_repo.clear_wallet_calls) == 1, (
        f"clear_wallet expected 1 call, got {users_repo.clear_wallet_calls!r}"
    )
    assert users_repo.clear_wallet_calls[0] == TELEGRAM_ID, (
        f"clear_wallet called with {users_repo.clear_wallet_calls[0]}, "
        f"expected {TELEGRAM_ID}"
    )

    assert fake_redis.glob(f"tc:session:{TELEGRAM_ID}:*") == [], (
        f"tc:session:{TELEGRAM_ID}:* keys must be gone, "
        f"still present: {fake_redis.glob(f'tc:session:{TELEGRAM_ID}:*')!r}"
    )
    assert not fake_redis.has_string(f"tc:nonce:{TELEGRAM_ID}"), (
        f"tc:nonce:{TELEGRAM_ID} must be gone, "
        f"strings={list(fake_redis._strings)}"
    )
    assert not fake_redis.has_hash(f"tc:connect_meta:{TELEGRAM_ID}"), (
        f"tc:connect_meta:{TELEGRAM_ID} must be gone, "
        f"hashes={list(fake_redis._hashes)}"
    )

    total_disconnects = sum(inst.disconnect_calls for inst in factory.instances)
    assert total_disconnects == 1, (
        f"tc.disconnect must be awaited exactly once across SDK instances, "
        f"got {total_disconnects} (per-instance: "
        f"{[i.disconnect_calls for i in factory.instances]})"
    )


async def test_disconnect_is_idempotent_after_failure(
    monkeypatch: pytest.MonkeyPatch,
    fake_redis: FakeRedis,
    users_repo: FakeUsersRepo,
    audit: FakeAuditLog,
    signing_key: SigningKey,
) -> None:
    """A post-failure disconnect does not raise and still wipes session keys (Req 3.5)."""
    factory = _install_fake_pytonconnect(monkeypatch)
    connector = _make_connector(fake_redis, users_repo, audit)

    await connector.start_connection(TELEGRAM_ID)
    fake_tc = factory.instances[0]
    assert fake_tc.last_connect_payload is not None
    # Trigger a ConnectionFailure via a tampered signature so the connector
    # pops itself from the in-process cache via its ``finally`` block.
    fake_tc.wallet = _build_wallet(
        signing_key=signing_key,
        payload=fake_tc.last_connect_payload,
        tamper_signature=True,
    )
    fake_tc.connected = True
    failure = await connector.await_connection(TELEGRAM_ID)
    assert isinstance(failure, ConnectionFailure), (
        f"precondition — expected ConnectionFailure, got {failure!r}"
    )
    assert failure.reason == "signature", (
        f"precondition — reason must be 'signature', got {failure.reason!r}"
    )

    # Seed residual session keys that the SDK would have written before the
    # user gave up. Subsequent disconnect must be a no-op on state but MUST
    # purge these keys regardless.
    await fake_redis.set(f"tc:session:{TELEGRAM_ID}:residual", "stale")

    # First disconnect: runs the fallback branch (no cached connector).
    await connector.disconnect(TELEGRAM_ID)
    # Second disconnect: MUST NOT raise even though there's nothing left.
    await connector.disconnect(TELEGRAM_ID)

    assert len(users_repo.clear_wallet_calls) == 2, (
        "clear_wallet must run on both idempotent disconnects, "
        f"got {users_repo.clear_wallet_calls!r}"
    )
    assert fake_redis.glob(f"tc:session:{TELEGRAM_ID}:*") == [], (
        "session keys must be gone after idempotent disconnect, "
        f"remaining: {fake_redis.glob(f'tc:session:{TELEGRAM_ID}:*')!r}"
    )
    assert not fake_redis.has_string(f"tc:nonce:{TELEGRAM_ID}"), (
        "nonce must be gone after idempotent disconnect"
    )


async def test_start_connection_requires_disconnect_first(
    monkeypatch: pytest.MonkeyPatch,
    fake_redis: FakeRedis,
    users_repo: FakeUsersRepo,
    audit: FakeAuditLog,
) -> None:
    """Second ``start_connection`` for a bound user is rejected; only one wallet allowed."""
    factory = _install_fake_pytonconnect(monkeypatch)
    # User already bound — subsequent start_connection must be short-circuited
    # without touching Redis or the SDK, even if a bogus call is issued.
    users_repo.preexisting[TELEGRAM_ID] = SimpleNamespace(
        ton_address="EQ" + "B" * 46,
        ton_wallet_name="Previously",
    )
    connector = _make_connector(fake_redis, users_repo, audit)

    with pytest.raises(AlreadyConnectedError):
        await connector.start_connection(TELEGRAM_ID)
    with pytest.raises(AlreadyConnectedError):
        await connector.start_connection(TELEGRAM_ID)

    assert users_repo.get_by_tg_id_calls == [TELEGRAM_ID, TELEGRAM_ID], (
        f"get_by_tg_id must be consulted on every attempt, "
        f"got {users_repo.get_by_tg_id_calls!r}"
    )
    assert factory.instances == [], (
        f"TonConnect must never be instantiated while user is bound, "
        f"instances={len(factory.instances)}"
    )
    assert not fake_redis._strings, (
        f"no Redis writes permitted while user is bound, got {fake_redis._strings!r}"
    )


# Expose the event loop policy expectation: tests rely on pyproject's
# asyncio_mode="auto" so async def tests run directly under pytest-asyncio.
_ = asyncio  # silence "imported but unused" when the module is type-checked.
