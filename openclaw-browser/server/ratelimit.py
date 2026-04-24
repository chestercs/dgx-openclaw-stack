"""Per-host token bucket — best-effort rate limiter at the application layer.

The MVP enforces this only on `/v1/actions` and `/v1/fetch`-style entrypoints.
Network-level enforcement (every subresource the page loads) would need
Chromium request interception, which is a Phase 2 enhancement. The signature
here stays the same when that lands — callers ask `try_acquire(host)` and get
a bool back.

Defaults from env: BROWSER_RATE_LIMIT_RPS=0.5, BROWSER_RATE_LIMIT_BURST=5.
"""
from __future__ import annotations

import os
import threading
import time
from collections import defaultdict
from dataclasses import dataclass


@dataclass
class _Bucket:
    tokens: float
    last_refill: float


class RateLimiter:
    def __init__(self, *, rps: float | None = None, burst: int | None = None) -> None:
        self.rps = rps if rps is not None else float(os.environ.get("BROWSER_RATE_LIMIT_RPS", "0.5"))
        self.burst = burst if burst is not None else int(os.environ.get("BROWSER_RATE_LIMIT_BURST", "5"))
        self._buckets: dict[str, _Bucket] = defaultdict(self._fresh_bucket)
        self._lock = threading.Lock()

    def _fresh_bucket(self) -> _Bucket:
        return _Bucket(tokens=float(self.burst), last_refill=time.monotonic())

    def try_acquire(self, host: str, *, cost: float = 1.0) -> bool:
        if self.rps <= 0:
            return True
        with self._lock:
            bucket = self._buckets[host]
            now = time.monotonic()
            elapsed = now - bucket.last_refill
            bucket.tokens = min(float(self.burst), bucket.tokens + elapsed * self.rps)
            bucket.last_refill = now
            if bucket.tokens >= cost:
                bucket.tokens -= cost
                return True
            return False

    def retry_after(self, host: str, *, cost: float = 1.0) -> float:
        with self._lock:
            bucket = self._buckets.get(host)
            if bucket is None or bucket.tokens >= cost or self.rps <= 0:
                return 0.0
            return max(0.0, (cost - bucket.tokens) / self.rps)
