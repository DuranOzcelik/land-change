"""Request throttling and response headers."""
from __future__ import annotations

import threading
import time
from collections import deque


class RateLimiter:
    """Sliding-window limit per key (client address)."""

    def __init__(self, limit: int, window_s: int):
        self.limit, self.window = limit, window_s
        self._hits: dict[str, deque] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> tuple[bool, int]:
        """Returns (allowed, seconds until the next request would be allowed)."""
        now = time.monotonic()
        with self._lock:
            if len(self._hits) > 5000:      # forget idle clients so memory stays bounded
                for k in [k for k, q in self._hits.items() if not q or now - q[-1] > self.window]:
                    del self._hits[k]
            hits = self._hits.setdefault(key, deque())
            while hits and now - hits[0] > self.window:
                hits.popleft()
            if len(hits) >= self.limit:
                return False, int(self.window - (now - hits[0])) + 1
            hits.append(now)
            return True, 0


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
