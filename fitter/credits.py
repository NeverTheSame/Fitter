"""Free-tier try-on allowance.

For now this is a single counter shared by every user and kept in memory,
so it resets on restart. Paid packs (Pack / Closet / Studio) are not modelled yet.
"""
from __future__ import annotations

import threading


class FreeTierLedger:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._used = 0
        self._lock = threading.Lock()

    def remaining(self, user_id: str) -> int:
        with self._lock:
            return max(0, self.limit - self._used)

    def try_consume(self, user_id: str) -> bool:
        """Take one try-on. False when the allowance is used up."""
        with self._lock:
            if self._used >= self.limit:
                return False
            self._used += 1
            return True

    def refund(self, user_id: str) -> None:
        """Give a try-on back, e.g. when the job failed before generating."""
        with self._lock:
            self._used = max(0, self._used - 1)
