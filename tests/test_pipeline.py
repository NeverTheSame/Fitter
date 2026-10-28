import logging
import unittest

from fitter.costs import CostLog
from fitter.credits import FreeTierLedger
from fitter.engines import EngineError, EngineResult, EngineTimeout, FakeEngine
from fitter.schemas import STEPS, Garment, JobStatus, TryOnJob
from fitter.store import InMemoryJobStore
from fitter.worker.pipeline import REFUNDED_NOTE, Pipeline
from tests.helpers import PNG_1PX


class RaisingEngine:
    """Raises a fresh copy of the given error on every call."""
    name = "raising"

    def __init__(self, make_error):
        self.make_error = make_error
        self.calls = 0

    def generate(self, selfie, garment) -> EngineResult:
        self.calls += 1
        raise self.make_error()


class RelabellingEngine:
    """Stands in for the router: its name differs from the engine that rendered."""
    name = "router"

    def generate(self, selfie, garment) -> EngineResult:
        return EngineResult(image=None, cost_usd=0.04, engine="fal")


def vendor_down():
    return EngineError("FASHN POST /v1/run -> HTTP 503: Service Unavailable", retryable=True)


def bad_input():
    return EngineError("FASHN prediction p1 failed: PoseError: No person detected")


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.store = InMemoryJobStore()
        self.costs = CostLog()
        self.ledger = FreeTierLedger(3)
        self.assertTrue(self.ledger.try_consume("u1"))  # the API takes the credit before queueing
        self.job = self.store.add(TryOnJob(user_id="u1", selfie=PNG_1PX, garment=Garment(image=PNG_1PX)))
        pipeline_log = logging.getLogger("fitter.pipeline")  # step failures are expected here
        self.addCleanup(pipeline_log.setLevel, pipeline_log.level)
        pipeline_log.setLevel(logging.CRITICAL)

    def pipeline(self, engine):
        return Pipeline(engine, self.store, self.costs, ledger=self.ledger)

    # --- success --------------------------------------------------------------

    def test_runs_all_steps_and_charges_generate_only(self):
        job = self.pipeline(FakeEngine(cost_usd=0.075)).run(self.job.id)
        self.assertEqual(job.status, JobStatus.DONE)
        self.assertEqual(job.step, "persist")
        self.assertAlmostEqual(job.cost_usd, 0.075)
        events = self.costs.for_job(self.job.id)
        self.assertEqual([e.step for e in events], list(STEPS))
        self.assertEqual([e.engine for e in events if e.cost_usd], ["fake"])

    def test_successful_job_does_not_refund(self):
        job = self.pipeline(FakeEngine()).run(self.job.id)
        self.assertEqual(job.status, JobStatus.DONE)
        self.assertFalse(job.refunded)
        self.assertIsNone(job.error)
        self.assertEqual(self.ledger.remaining("u1"), 2)

    def test_generate_cost_is_billed_to_the_engine_that_rendered(self):
        self.pipeline(RelabellingEngine()).run(self.job.id)
        events = self.costs.for_job(self.job.id)
        self.assertEqual([e.engine for e in events if e.cost_usd], ["fal"])
        self.assertAlmostEqual(self.costs.total_usd(), 0.04)

    # --- failure: refund once, user-facing reason, no phantom cost ---------------

    def test_engine_error_fails_job_and_refunds_once(self):
        engine = RaisingEngine(vendor_down)
        pipeline = self.pipeline(engine)
        job = pipeline.run(self.job.id)

        self.assertEqual(job.status, JobStatus.FAILED)
        self.assertTrue(job.refunded)
        self.assertEqual(self.ledger.remaining("u1"), 3)
        self.assertEqual(job.error, "The try-on service is unavailable right now. Please try again in a few minutes. "
                                    + REFUNDED_NOTE)
        self.assertNotIn("HTTP", job.error)  # vendor detail is for logs, not users
        self.assertEqual(job.error_detail, "generate: FASHN POST /v1/run -> HTTP 503: Service Unavailable")
        self.assertEqual(job.cost_usd, 0.0)
        self.assertEqual([e.step for e in self.costs.for_job(job.id)], ["acquire_garment", "check_inputs"])
        self.assertEqual(self.costs.total_usd(), 0.0)

        # The worker retries the same job: nothing runs again, nothing is refunded again.
        again = pipeline.run(self.job.id)
        self.assertEqual(again.status, JobStatus.FAILED)
        self.assertEqual(engine.calls, 1)
        self.assertEqual(self.ledger.remaining("u1"), 3)

    def test_bad_input_error_explains_and_refunds(self):
        job = self.pipeline(RaisingEngine(bad_input)).run(self.job.id)
        self.assertEqual(job.status, JobStatus.FAILED)
        self.assertIn("couldn't work with these photos", job.error)
        self.assertIn(REFUNDED_NOTE, job.error)
        self.assertEqual(self.ledger.remaining("u1"), 3)

    def test_timeout_refunds_once_and_records_the_estimated_charge(self):
        def billed_timeout():
            exc = EngineTimeout("FASHN prediction p1 not finished after 90s")
            exc.estimated_cost_usd, exc.engine = 0.075, "fashn"
            return exc

        pipeline = self.pipeline(RaisingEngine(billed_timeout))
        job = pipeline.run(self.job.id)

        self.assertEqual(job.status, JobStatus.FAILED)
        self.assertEqual(job.error, "The try-on took too long and was cancelled. Please try again. " + REFUNDED_NOTE)
        self.assertTrue(job.refunded)
        self.assertEqual(self.ledger.remaining("u1"), 3)
        # The engine thinks the vendor billed the render we gave up on: logged
        # as an estimate, kept out of confirmed totals and out of the job's cost.
        billed = [e for e in self.costs.for_job(job.id) if e.cost_usd]
        self.assertEqual([(e.step, e.engine, e.cost_usd, e.estimated) for e in billed],
                         [("generate", "fashn", 0.075, True)])
        self.assertEqual(job.cost_usd, 0.0)
        self.assertEqual(self.costs.total_usd(), 0.0)
        self.assertAlmostEqual(self.costs.estimated_usd(), 0.075)

        pipeline.run(self.job.id)
        self.assertEqual(self.ledger.remaining("u1"), 3)
        self.assertAlmostEqual(self.costs.estimated_usd(), 0.075)

    def test_timeout_without_billing_records_no_cost(self):
        job = self.pipeline(RaisingEngine(lambda: EngineTimeout("fal request r1 not finished after 90s"))).run(self.job.id)
        self.assertEqual(job.status, JobStatus.FAILED)
        self.assertEqual((self.costs.total_usd(), self.costs.estimated_usd()), (0.0, 0.0))
        self.assertEqual(self.ledger.remaining("u1"), 3)

    def test_refund_is_claimed_atomically_even_if_forced_to_run_twice(self):
        # Belt and braces: even a job that somehow runs again cannot refund twice.
        job = self.pipeline(RaisingEngine(vendor_down)).run(self.job.id)
        self.assertTrue(job.refunded)
        self.store.update(job.id, status=JobStatus.QUEUED)  # simulate a stale requeue
        self.pipeline(RaisingEngine(vendor_down)).run(self.job.id)
        self.assertEqual(self.ledger.remaining("u1"), 3)
        self.assertFalse(self.store.claim_refund(job.id))

    def test_unexpected_exception_also_fails_safely(self):
        job = self.pipeline(RaisingEngine(lambda: KeyError("output"))).run(self.job.id)
        self.assertEqual(job.status, JobStatus.FAILED)
        self.assertTrue(job.error.startswith("Something went wrong on our side."))
        self.assertEqual(self.ledger.remaining("u1"), 3)

    def test_without_a_ledger_nothing_is_refunded(self):
        job = Pipeline(RaisingEngine(vendor_down), self.store, self.costs).run(self.job.id)
        self.assertEqual(job.status, JobStatus.FAILED)
        self.assertFalse(job.refunded)
        self.assertNotIn(REFUNDED_NOTE, job.error)

    def test_done_job_is_not_re_run(self):
        engine = FakeEngine()
        pipeline = self.pipeline(engine)
        first = pipeline.run(self.job.id)
        second = pipeline.run(self.job.id)
        self.assertIs(first, second)
        self.assertEqual(len(self.costs.for_job(self.job.id)), len(STEPS))


if __name__ == "__main__":
    unittest.main()
