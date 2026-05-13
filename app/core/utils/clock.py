"""Injectable clock so tests can freeze/shift time.

Using a callable typed as ``Clock`` throughout the code lets us replace the
real ``utc_now`` with a controllable fake in tests without monkey-patching
``datetime``.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    """Return the current UTC timestamp as a timezone-aware datetime."""
    return datetime.now(tz=UTC)
