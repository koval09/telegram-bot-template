"""Contract tests against mocked external APIs (TON Connect, TonCenter, etc.).

These tests live behind the ``contract`` pytest marker (see ``pyproject.toml``)
and never reach the network: every external SDK is replaced by an in-process
fake or ``monkeypatch``-installed stub.
"""
