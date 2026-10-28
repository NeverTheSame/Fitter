"""Router + CircuitBreaker with scripted in-memory engines; no HTTP."""
import logging
import unittest

from fitter.engines import EngineError, EngineResult, EngineTimeout
from fitter.router import CircuitBreaker, Router
from fitter.schemas import Garment
from tests.helpers import PNG_1PX

OK = "https://cdn.example/render.png"


def down():
    return EngineError("HTTP 503", retryable=True)


def broke():
    return EngineError("HTTP 402 out of credits", failover=True)


def bad():
    return EngineError("PoseError: no person", retryable=False)


def timeout():
    return EngineTimeout("not finished after 90s")


class ScriptedEngine:
    """Outcomes in order; the last one repeats. A str is a render, an exception is raised."""

    def __init__(self, name, *outcomes, cost_usd=0.01):
        self.name = name
        self.outcomes = list(outcomes) or [OK]
        self.cost_usd = cost_usd
        self.calls = 0

    def generate(self, selfie, garment):
        self.calls += 1
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, BaseException):
            raise outcome
        return EngineResult(image=outcome, cost_usd=self.cost_usd, engine=self.name)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class RouterTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.garment = Garment(image=PNG_1PX)
        # Failover warnings are expected here; assertLogs still captures them.
        router_log = logging.getLogger("fitter.router")
        self.addCleanup(router_log.setLevel, router_log.level)
        router_log.setLevel(logging.CRITICAL)

    def router(self, primary, fallback, threshold=3, cooldown_s=60.0):
        return Router(primary, fallback, failure_threshold=threshold, cooldown_s=cooldown_s, clock=self.clock)

    def go(self, router):
        return router.generate(PNG_1PX, self.garment)

    # --- routing ------------------------------------------------------------

    def test_uses_primary_when_it_works(self):
        primary, fallback = ScriptedEngine("fashn", OK, cost_usd=0.075), ScriptedEngine("fal")
        result = self.go(self.router(primary, fallback))
        self.assertEqual((result.image, result.engine, result.cost_usd), (OK, "fashn", 0.075))
        self.assertEqual((primary.calls, fallback.calls), (1, 0))

    def test_fails_over_when_primary_is_down(self):
        primary, fallback = ScriptedEngine("fashn", down()), ScriptedEngine("fal", OK, cost_usd=0.04)
        with self.assertLogs("fitter.router", level="WARNING") as logs:
            result = self.go(self.router(primary, fallback))
        self.assertEqual((result.engine, result.cost_usd), ("fal", 0.04))
        self.assertEqual((primary.calls, fallback.calls), (1, 1))
        self.assertIn("engine=fashn failed, failing over: HTTP 503", logs.output[0])

    def test_fails_over_when_primary_account_is_broken(self):
        primary, fallback = ScriptedEngine("fashn", broke()), ScriptedEngine("fal")
        self.assertEqual(self.go(self.router(primary, fallback)).engine, "fal")

    def test_does_not_fail_over_on_bad_input(self):
        primary, fallback = ScriptedEngine("fashn", bad()), ScriptedEngine("fal")
        with self.assertRaises(EngineError) as ctx:
            self.go(self.router(primary, fallback))
        self.assertIn("PoseError", str(ctx.exception))
        self.assertFalse(ctx.exception.failover)
        self.assertEqual(fallback.calls, 0)

    def test_does_not_fail_over_after_timeout(self):
        primary, fallback = ScriptedEngine("fashn", timeout()), ScriptedEngine("fal")
        with self.assertRaises(EngineTimeout):
            self.go(self.router(primary, fallback))
        self.assertEqual(fallback.calls, 0)

    def test_both_down_raises_one_error_naming_both(self):
        primary, fallback = ScriptedEngine("fashn", down()), ScriptedEngine("fal", broke())
        with self.assertRaises(EngineError) as ctx:
            self.go(self.router(primary, fallback))
        msg = str(ctx.exception)
        self.assertIn("fashn: HTTP 503", msg)
        self.assertIn("fal: HTTP 402", msg)
        # flags are OR-ed: fashn was retryable, fal only failover
        self.assertTrue(ctx.exception.retryable)
        self.assertTrue(ctx.exception.failover)

    def test_both_account_broken_is_failover_but_not_retryable(self):
        primary, fallback = ScriptedEngine("fashn", broke()), ScriptedEngine("fal", broke())
        with self.assertRaises(EngineError) as ctx:
            self.go(self.router(primary, fallback))
        self.assertEqual((ctx.exception.retryable, ctx.exception.failover), (False, True))

    def test_bad_input_judged_by_fallback_is_raised_as_is(self):
        primary, fallback = ScriptedEngine("fashn", down()), ScriptedEngine("fal", bad())
        with self.assertRaises(EngineError) as ctx:
            self.go(self.router(primary, fallback))
        self.assertEqual(str(ctx.exception), "PoseError: no person")

    def test_fallback_timeout_is_raised_as_is(self):
        primary, fallback = ScriptedEngine("fashn", down()), ScriptedEngine("fal", timeout())
        with self.assertRaises(EngineTimeout):
            self.go(self.router(primary, fallback))

    def test_unexpected_exception_propagates_without_failover(self):
        primary, fallback = ScriptedEngine("fashn", KeyError("output")), ScriptedEngine("fal")
        router = self.router(primary, fallback, threshold=1)
        with self.assertRaises(KeyError):
            self.go(router)
        self.assertEqual(fallback.calls, 0)
        self.assertTrue(router.status()["fashn"]["open"])  # but it did count

    def test_rejects_engines_with_the_same_name(self):
        with self.assertRaises(ValueError):
            Router(ScriptedEngine("x"), ScriptedEngine("x"))

    # --- circuit breaker through the router -----------------------------------

    def test_primary_is_skipped_after_n_failures_in_a_row(self):
        primary, fallback = ScriptedEngine("fashn", down()), ScriptedEngine("fal")
        router = self.router(primary, fallback, threshold=2)
        with self.assertLogs("fitter.router", level="WARNING") as logs:
            for _ in range(5):
                self.assertEqual(self.go(router).engine, "fal")
        self.assertEqual((primary.calls, fallback.calls), (2, 5))
        self.assertEqual(router.status(), {"fashn": {"failures": 2, "open": True}, "fal": {"failures": 0, "open": False}})
        self.assertEqual(sum("circuit opened after 2 failures" in line for line in logs.output), 1)

    def test_one_probe_after_cooldown_then_closed_on_success(self):
        primary, fallback = ScriptedEngine("fashn", down(), down(), OK), ScriptedEngine("fal")
        router = self.router(primary, fallback, threshold=2, cooldown_s=60)
        for _ in range(3):
            self.go(router)
        self.assertEqual(primary.calls, 2)

        self.clock.now += 59  # still cooling down
        self.go(router)
        self.assertEqual(primary.calls, 2)

        self.clock.now += 1  # cooldown over: one request goes through and succeeds
        with self.assertLogs("fitter.router", level="INFO") as logs:
            self.assertEqual(self.go(router).engine, "fashn")
        self.assertIn("probe succeeded, circuit closed", logs.output[0])
        self.assertEqual(router.status()["fashn"], {"failures": 0, "open": False})
        self.assertEqual(self.go(router).engine, "fashn")
        self.assertEqual(primary.calls, 4)

    def test_failed_probe_reopens_for_another_cooldown(self):
        primary, fallback = ScriptedEngine("fashn", down()), ScriptedEngine("fal")
        router = self.router(primary, fallback, threshold=2, cooldown_s=60)
        for _ in range(3):
            self.go(router)
        self.assertEqual(primary.calls, 2)

        self.clock.now += 60
        self.go(router)  # probe, fails
        self.assertEqual(primary.calls, 3)
        self.clock.now += 30
        self.go(router)  # skipped again
        self.assertEqual(primary.calls, 3)
        self.clock.now += 30
        self.go(router)  # next probe
        self.assertEqual(primary.calls, 4)
        self.assertTrue(router.status()["fashn"]["open"])

    def test_success_resets_the_streak(self):
        primary, fallback = ScriptedEngine("fashn", down(), OK, down(), OK, down(), OK), ScriptedEngine("fal")
        router = self.router(primary, fallback, threshold=2)
        engines = [self.go(router).engine for _ in range(6)]
        self.assertEqual(engines, ["fal", "fashn", "fal", "fashn", "fal", "fashn"])
        self.assertFalse(router.status()["fashn"]["open"])

    def test_bad_inputs_do_not_count_towards_the_breaker(self):
        primary, fallback = ScriptedEngine("fashn", bad()), ScriptedEngine("fal")
        router = self.router(primary, fallback, threshold=2)
        for _ in range(5):
            with self.assertRaises(EngineError):
                self.go(router)
        self.assertEqual(primary.calls, 5)
        self.assertEqual(router.status()["fashn"], {"failures": 0, "open": False})

    def test_bad_input_answer_ends_the_streak(self):
        primary, fallback = ScriptedEngine("fashn", down(), bad(), down(), OK), ScriptedEngine("fal")
        router = self.router(primary, fallback, threshold=2)
        self.go(router)                     # down -> fal, streak 1
        with self.assertRaises(EngineError):
            self.go(router)                 # bad input: engine answered, streak reset
        self.go(router)                     # down -> fal, streak 1, still closed
        self.assertFalse(router.status()["fashn"]["open"])
        self.assertEqual(self.go(router).engine, "fashn")

    def test_timeouts_count_towards_the_breaker(self):
        primary, fallback = ScriptedEngine("fashn", timeout()), ScriptedEngine("fal")
        router = self.router(primary, fallback, threshold=2)
        for _ in range(2):
            with self.assertRaises(EngineTimeout):
                self.go(router)
        self.assertEqual(self.go(router).engine, "fal")
        self.assertEqual(primary.calls, 2)

    def test_fallback_has_its_own_breaker(self):
        primary, fallback = ScriptedEngine("fashn", down()), ScriptedEngine("fal", down())
        router = self.router(primary, fallback, threshold=1, cooldown_s=60)
        with self.assertRaises(EngineError) as ctx:
            self.go(router)
        self.assertIn("all engines failed", str(ctx.exception))
        self.assertEqual(router.status(), {"fashn": {"failures": 1, "open": True}, "fal": {"failures": 1, "open": True}})

        with self.assertRaises(EngineError) as ctx:
            self.go(router)  # nothing is tried
        self.assertIn("no engine available", str(ctx.exception))
        self.assertTrue(ctx.exception.retryable)
        self.assertEqual((primary.calls, fallback.calls), (1, 1))

    def test_skipped_primary_is_named_when_fallback_fails(self):
        primary, fallback = ScriptedEngine("fashn", down()), ScriptedEngine("fal", OK, down())
        router = self.router(primary, fallback, threshold=1)
        self.go(router)  # opens fashn, fal serves
        with self.assertRaises(EngineError) as ctx:
            self.go(router)
        self.assertIn("fal: HTTP 503", str(ctx.exception))
        self.assertIn("skipped, cooling down: fashn", str(ctx.exception))


class CircuitBreakerTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.breaker = CircuitBreaker(threshold=2, cooldown_s=10, clock=self.clock)

    def test_closed_until_threshold(self):
        self.assertTrue(self.breaker.allow())
        self.assertFalse(self.breaker.record_failure())
        self.assertTrue(self.breaker.allow())
        self.assertTrue(self.breaker.record_failure())
        self.assertTrue(self.breaker.is_open)
        self.assertFalse(self.breaker.allow())

    def test_half_open_lets_exactly_one_probe_through(self):
        self.breaker.record_failure()
        self.breaker.record_failure()
        self.clock.now += 10
        self.assertTrue(self.breaker.allow())   # the probe
        self.assertFalse(self.breaker.allow())  # anyone else waits for its verdict
        self.assertFalse(self.breaker.allow())
        self.breaker.record_success()
        self.assertFalse(self.breaker.is_open)
        self.assertTrue(self.breaker.allow())
        self.assertTrue(self.breaker.allow())

    def test_failed_probe_frees_the_slot_after_next_cooldown(self):
        self.breaker.record_failure()
        self.breaker.record_failure()
        self.clock.now += 10
        self.assertTrue(self.breaker.allow())
        self.breaker.record_failure()
        self.assertFalse(self.breaker.allow())
        self.clock.now += 10
        self.assertTrue(self.breaker.allow())

    def test_rejects_bad_arguments(self):
        with self.assertRaises(ValueError):
            CircuitBreaker(threshold=0, cooldown_s=1)
        with self.assertRaises(ValueError):
            CircuitBreaker(threshold=1, cooldown_s=-1)


if __name__ == "__main__":
    unittest.main()
