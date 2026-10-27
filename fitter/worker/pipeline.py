"""The try-on pipeline: five typed steps, run in order for one job."""
from __future__ import annotations

import logging
import time
from typing import Callable

from ..costs import CostLog
from ..engine import EngineResult, TryOnEngine
from ..schemas import STEPS, CostEvent, JobStatus, TryOnJob
from ..store import InMemoryJobStore

log = logging.getLogger("fitter.pipeline")


class StepFailed(RuntimeError):
    pass


class Pipeline:
    def __init__(
        self,
        engine: TryOnEngine,
        store: InMemoryJobStore,
        costs: CostLog,
        step_delay_s: float = 0.0,
        on_failed: Callable[[TryOnJob], None] | None = None,
    ) -> None:
        self.engine = engine
        self.store = store
        self.costs = costs
        self.step_delay_s = step_delay_s
        self.on_failed = on_failed
        self._result: EngineResult | None = None

    def run(self, job_id: str) -> TryOnJob:
        job = self.store.get(job_id)
        if job is None:
            raise StepFailed(f"job {job_id} not found")
        self._result = None
        for step in STEPS:
            job = self.store.update(job_id, status=JobStatus.RUNNING, step=step)
            try:
                cost = getattr(self, step)(job)
            except Exception as exc:  # any step failure fails the job
                log.warning("job=%s step=%s failed: %s", job_id, step, exc)
                job = self.store.update(job_id, status=JobStatus.FAILED, error=f"{step}: {exc}")
                if self.on_failed:
                    self.on_failed(job)
                return job
            self.costs.record(CostEvent(job_id=job_id, step=step, cost_usd=cost,
                                        engine=self.engine.name if step == "generate" else None))
            self.store.update(job_id, cost_usd=job.cost_usd + cost)
            if self.step_delay_s:
                time.sleep(self.step_delay_s)
        return self.store.update(job_id, status=JobStatus.DONE)

    # Each step takes the job and returns its cost in USD.

    def acquire_garment(self, job: TryOnJob) -> float:
        # Garments arrive already resolved (upload or /api/garments/from-url).
        if not job.garment.image:
            raise StepFailed("garment has no image")
        return 0.0

    def check_inputs(self, job: TryOnJob) -> float:
        # Placeholder for the input guardrails (person present, not a minor, etc.).
        return 0.0

    def generate(self, job: TryOnJob) -> float:
        self._result = self.engine.generate(job.selfie, job.garment)
        return self._result.cost_usd

    def check_output(self, job: TryOnJob) -> float:
        # Placeholder for the output check (garment applied, face unchanged).
        if self._result is None:
            raise StepFailed("generate produced no result")
        return 0.0

    def persist(self, job: TryOnJob) -> float:
        self.store.update(job.id, render=self._result.image if self._result else None)
        return 0.0
