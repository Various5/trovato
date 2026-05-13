"""Tiny in-memory rate limiter for the login endpoint.

Sliding window of failures per (IP, username). After ``max_attempts`` failures
within ``window_seconds``, the (IP, username) is locked out for
``lockout_seconds``.

This is *not* a substitute for a real distributed rate limiter; for the local
single-process desktop app it stops casual brute-forcing.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass


@dataclass
class _Entry:
    failures: deque[float]
    locked_until: float = 0.0


_STATE: dict[tuple[str, str], _Entry] = defaultdict(lambda: _Entry(deque(maxlen=20)))

MAX_ATTEMPTS = 5
WINDOW_SECONDS = 5 * 60  # 5 min
LOCKOUT_SECONDS = 15 * 60  # 15 min


def _key(ip: str, username: str) -> tuple[str, str]:
    return ip or "unknown", (username or "").lower()


def is_locked(ip: str, username: str) -> tuple[bool, float]:
    """Return ``(locked, retry_after_seconds)``."""
    e = _STATE[_key(ip, username)]
    now = time.time()
    if e.locked_until > now:
        return True, e.locked_until - now
    return False, 0.0


def record_failure(ip: str, username: str) -> tuple[bool, float]:
    e = _STATE[_key(ip, username)]
    now = time.time()
    # Drop old entries outside the window
    while e.failures and now - e.failures[0] > WINDOW_SECONDS:
        e.failures.popleft()
    e.failures.append(now)
    if len(e.failures) >= MAX_ATTEMPTS:
        e.locked_until = now + LOCKOUT_SECONDS
        return True, LOCKOUT_SECONDS
    return False, 0.0


def record_success(ip: str, username: str) -> None:
    _STATE.pop(_key(ip, username), None)
