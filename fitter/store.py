"""Job storage. In memory only: jobs are lost when the process exits."""
from __future__ import annotations

import threading
from typing import Optional

from .schemas import TryOnJob, utcnow


class JobNotFound(KeyError):
    pass


class InMemoryJobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, TryOnJob] = {}
        self._lock = threading.Lock()

    def add(self, job: TryOnJob) -> TryOnJob:
        with self._lock:
            self._jobs[job.id] = job
        return job

    def get(self, job_id: str) -> Optional[TryOnJob]:
        with self._lock:
            return self._jobs.get(job_id)

    def update(self, job_id: str, **changes) -> TryOnJob:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise JobNotFound(job_id)
            for key, value in changes.items():
                if not hasattr(job, key):
                    raise AttributeError(f"TryOnJob has no field {key!r}")
                setattr(job, key, value)
            job.updated_at = utcnow()
            return job

    def claim_refund(self, job_id: str) -> bool:
        """Mark the job's credit as refunded. True the first time, False after
        that, atomically, so a refund cannot be paid twice for one job."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise JobNotFound(job_id)
            if job.refunded:
                return False
            job.refunded = True
            job.updated_at = utcnow()
            return True

    def list_for_user(self, user_id: str) -> list[TryOnJob]:
        with self._lock:
            jobs = [j for j in self._jobs.values() if j.user_id == user_id]
        return sorted(jobs, key=lambda j: j.created_at, reverse=True)
