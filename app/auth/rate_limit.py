"""Tiny in-memory rate limiter for the login endpoint.

Sliding window of failures per (IP, username). After ``max_attempts`` failures
within ``window_seconds``, the (IP, username) is locked out for
``lockout_seconds``.

This is *not* a substitute for a real distributed rate limiter; for the local
single-process desktop app it stops casual brute-forcing.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field


@dataclass
class _Entry:
    failures: deque[float] = field(default_factory=lambda: deque(maxlen=20))
    locked_until: float = 0.0


# Plain dict (NOT defaultdict): is_locked must not silently allocate an entry
# for every username it's ever asked about, or an attacker who POSTs /login with
# millions of unique usernames grows this map without bound until OOM. Entries
# are created only on a real failure, swept when their window expires, and the
# map size is hard-capped as a backstop.
_STATE: dict[tuple[str, str], _Entry] = {}

MAX_ATTEMPTS = 5
WINDOW_SECONDS = 5 * 60  # 5 min
LOCKOUT_SECONDS = 15 * 60  # 15 min
_MAX_KEYS = 50_000  # backstop against state-map flooding


def _key(ip: str, username: str) -> tuple[str, str]:
    # Cap the username length so a giant string can't bloat a map key.
    return ip or "unknown", (username or "").lower()[:128]


def _sweep(now: float) -> None:
    """Drop entries that are no longer locked and whose window has expired."""
    stale = [
        k
        for k, e in _STATE.items()
        if e.locked_until <= now and (not e.failures or now - e.failures[-1] > WINDOW_SECONDS)
    ]
    for k in stale:
        _STATE.pop(k, None)


def is_locked(ip: str, username: str) -> tuple[bool, float]:
    """Return ``(locked, retry_after_seconds)``. Never allocates a new entry."""
    e = _STATE.get(_key(ip, username))
    if e is None:
        return False, 0.0
    now = time.time()
    if e.locked_until > now:
        return True, e.locked_until - now
    return False, 0.0


def record_failure(ip: str, username: str) -> tuple[bool, float]:
    now = time.time()
    key = _key(ip, username)
    e = _STATE.get(key)
    if e is None:
        if len(_STATE) >= _MAX_KEYS:
            _sweep(now)
        if len(_STATE) >= _MAX_KEYS:
            # Still full of active lockouts — refuse to grow further; the
            # offending traffic is already being throttled elsewhere.
            return False, 0.0
        e = _STATE[key] = _Entry()
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
