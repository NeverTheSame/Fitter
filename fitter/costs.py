"""Per-step cost events. Logged and kept in memory; no exporter yet."""
from __future__ import annotations

import logging
import threading

from .schemas import CostEvent

log = logging.getLogger("fitter.costs")


class CostLog:
    def __init__(self) -> None:
        self._events: list[CostEvent] = []
        self._lock = threading.Lock()

    def record(self, event: CostEvent) -> None:
        with self._lock:
            self._events.append(event)
        log.info("job=%s step=%s engine=%s cost_usd=%.4f",
                 event.job_id, event.step, event.engine, event.cost_usd)

    def for_job(self, job_id: str) -> list[CostEvent]:
        with self._lock:
            return [e for e in self._events if e.job_id == job_id]

    def total_usd(self) -> float:
        with self._lock:
            return sum(e.cost_usd for e in self._events)
