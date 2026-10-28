"""The try-on pipeline: five typed steps, run in order for one job."""
from __future__ import annotations

import logging
import time
from typing import Optional

from ..costs import CostLog
from ..credits import FreeTierLedger
from ..engines import EngineError, EngineResult, EngineTimeout, TryOnEngine
from ..schemas import STEPS, CostEvent, JobStatus, TryOnJob
from ..store import InMemoryJobStore

log = logging.getLogger("fitter.pipeline")

_TERMINAL = (JobStatus.DONE, JobStatus.FAILED)
REFUNDED_NOTE = "Your try-on credit has been returned."


class StepFailed(RuntimeError):
    pass


def failure_reason(exc: BaseException) -> str:
    """What the user is told. Vendor and HTTP details stay in error_detail and the log."""
    if isinstance(exc, EngineTimeout):
        return "The try-on took too long and was cancelled. Please try again."
    if isinstance(exc, EngineError):
        if exc.failover:
            return "The try-on service is unavailable right now. Please try again in a few minutes."
        return "The try-on engine couldn't work with these photos. Use a clear, front-facing photo of one person and one garment."
    return "Something went wrong on our side. Please try again."


class Pipeline:
    def __init__(
        self,
        engine: TryOnEngine,
        store: InMemoryJobStore,
        costs: CostLog,
        ledger: Optional[FreeTierLedger] = None,
        step_delay_s: float = 0.0,
    ) -> None:
        self.engine = engine
        self.store = store
        self.costs = costs
        self.ledger = ledger
        self.step_delay_s = step_delay_s
        self._result: EngineResult | None = None

    def run(self, job_id: str) -> TryOnJob:
        job = self.store.get(job_id)
        if job is None:
            raise StepFailed(f"job {job_id} not found")
        if job.status in _TERMINAL:
            # A retried or duplicated job id must not render, charge or refund again.
            log.info("job=%s already %s, not re-running", job_id, job.status.value)
            return job
        self._result = None
        for step in STEPS:
            job = self.store.update(job_id, status=JobStatus.RUNNING, step=step)
            try:
                cost = getattr(self, step)(job)
            except Exception as exc:  # any step failure fails the job
                return self._fail(job, step, exc)
            self.costs.record(CostEvent(job_id=job_id, step=step, cost_usd=cost,
                                        engine=self._engine_used() if step == "generate" else None))
            self.store.update(job_id, cost_usd=job.cost_usd + cost)
            if self.step_delay_s:
                time.sleep(self.step_delay_s)
        return self.store.update(job_id, status=JobStatus.DONE)

    def _fail(self, job: TryOnJob, step: str, exc: BaseException) -> TryOnJob:
        """Fail the job with a user-facing reason, note what the vendor may
        have billed anyway, and give the credit back exactly once."""
        detail = f"{step}: {exc}"
        log.warning("job=%s step=%s failed: %s", job.id, step, exc)
        if isinstance(exc, EngineError) and exc.estimated_cost_usd:
            # An estimate, not a confirmed charge: it goes to the cost log
            # flagged as such and does not count towards the job's cost.
            self.costs.record(CostEvent(job_id=job.id, step=step, cost_usd=exc.estimated_cost_usd,
                                        engine=exc.engine or self.engine.name, estimated=True))
        reason = failure_reason(exc)
        if self.ledger is not None and self.store.claim_refund(job.id):
            self.ledger.refund(job.user_id)
            log.info("job=%s credit refunded", job.id)
            reason = f"{reason} {REFUNDED_NOTE}"
        return self.store.update(job.id, status=JobStatus.FAILED, error=reason, error_detail=detail)

    def _engine_used(self) -> str:
        # Behind a router, the engine that rendered is the one to bill, not the router.
        if self._result is not None and self._result.engine:
            return self._result.engine
        return self.engine.name

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
