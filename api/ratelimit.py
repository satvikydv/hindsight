import threading
import time
from collections import defaultdict, deque


class RateLimiter:
    """Sliding window: at most `limit` hits per `window_s` seconds per key (PRD §11: 10 incidents per IP per hour)."""

    def __init__(self, limit: int, window_s: float = 3600.0, clock=time.monotonic):
        self.limit, self.window_s, self.clock = limit, window_s, clock
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = self.clock()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] >= self.window_s:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(now)
            return True

    def retry_after(self, key: str) -> int:
        with self._lock:
            q = self._hits[key]
            return max(1, int(self.window_s - (self.clock() - q[0]))) if q else 0
