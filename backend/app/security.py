"""Request throttling and response headers."""
from __future__ import annotations

import threading
import time
from collections import deque


class RateLimiter:
    """Sliding-window limit per key (client address). A request can cost more than one unit."""

    def __init__(self, limit: int, window_s: int):
        self.limit, self.window = limit, window_s
        self._hits: dict[str, deque] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, cost: int = 1) -> tuple[bool, int]:
        """Returns (allowed, seconds until the request would be allowed)."""
        now = time.monotonic()
        with self._lock:
            if len(self._hits) > 5000:      # forget idle clients so memory stays bounded
                for k in [k for k, q in self._hits.items() if not q or now - q[-1] > self.window]:
                    del self._hits[k]
            hits = self._hits.setdefault(key, deque())
            while hits and now - hits[0] > self.window:
                hits.popleft()
            if len(hits) + cost > self.limit:
                if not hits:
                    return False, self.window
                # wait until enough old units have left the window
                oldest = hits[min(len(hits) - 1, len(hits) + cost - self.limit - 1)]
                return False, int(self.window - (now - oldest)) + 1
            hits.extend([now] * cost)
            return True, 0


class DailyQuota:
    """A cap on the work the whole service does per UTC day, whoever asks for it.

    This bounds the cost of the service: once the day's units are used, analyses are refused until
    midnight UTC. It lives in memory, which is exact while Cloud Run runs a single instance
    (max-instances 1); a restart starts the day's count again.
    """

    def __init__(self, limit: int, clock=time.time):
        self.limit = limit
        self._clock = clock
        self._day = None
        self._used = 0
        self._lock = threading.Lock()

    def _roll(self) -> int:
        now = self._clock()
        day = int(now // 86400)
        if day != self._day:
            self._day, self._used = day, 0
        return int((day + 1) * 86400 - now) + 1          # seconds until midnight UTC

    def take(self, units: int) -> tuple[bool, int]:
        """Reserve units; returns (allowed, seconds until the quota resets)."""
        with self._lock:
            reset = self._roll()
            if self._used + units > self.limit:
                return False, reset
            self._used += units
            return True, 0

    def give_back(self, units: int) -> None:
        """Return units of a request that did no work (e.g. it was rejected as invalid)."""
        with self._lock:
            self._roll()
            self._used = max(0, self._used - units)

    @property
    def remaining(self) -> int:
        with self._lock:
            self._roll()
            return max(0, self.limit - self._used)


def client_address(headers, fallback: str = "unknown") -> str:
    """Cloud Run appends the caller's address to X-Forwarded-For; earlier entries can be forged."""
    forwarded = headers.get("x-forwarded-for", "")
    last = forwarded.split(",")[-1].strip()
    return last or fallback


SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "Permissions-Policy": "geolocation=(), camera=(), microphone=(), payment=()",
    "Content-Security-Policy": "frame-ancestors 'none'",   # the page adds its own, stricter policy
    "Cross-Origin-Opener-Policy": "same-origin",
}
