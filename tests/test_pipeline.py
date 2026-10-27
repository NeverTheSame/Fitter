import unittest

from fitter.costs import CostLog
from fitter.engine import EngineResult, FakeEngine
from fitter.schemas import STEPS, Garment, JobStatus, TryOnJob
from fitter.store import InMemoryJobStore
from fitter.worker.pipeline import Pipeline
from tests.helpers import PNG_1PX


class BrokenEngine:
    name = "broken"

    def generate(self, selfie, garment) -> EngineResult:
        raise RuntimeError("upstream 503")


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.store = InMemoryJobStore()
        self.costs = CostLog()
        self.job = self.store.add(TryOnJob(user_id="u1", selfie=PNG_1PX, garment=Garment(image=PNG_1PX)))

    def test_runs_all_steps_and_charges_generate_only(self):
        job = Pipeline(FakeEngine(cost_usd=0.075), self.store, self.costs).run(self.job.id)
        self.assertEqual(job.status, JobStatus.DONE)
        self.assertEqual(job.step, "persist")
        self.assertAlmostEqual(job.cost_usd, 0.075)
        events = self.costs.for_job(self.job.id)
        self.assertEqual([e.step for e in events], list(STEPS))
        self.assertEqual([e.engine for e in events if e.cost_usd], ["fake"])

    def test_engine_failure_fails_job_and_calls_hook(self):
        failed = []
        job = Pipeline(BrokenEngine(), self.store, self.costs, on_failed=failed.append).run(self.job.id)
        self.assertEqual(job.status, JobStatus.FAILED)
        self.assertIn("generate", job.error)
        self.assertEqual(len(failed), 1)


if __name__ == "__main__":
    unittest.main()
