"""Engine router: a primary engine with a fallback and a circuit breaker each.

Router is itself a TryOnEngine, so the pipeline can hold one without knowing
how many engines sit behind it. build_engine() turns Settings into whichever
engine FITTER_ENGINE asks for.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

from .config import ENGINES, ConfigError, Settings
from .engines import EngineError, EngineResult, EngineTimeout, FakeEngine, FalEngine, FashnEngine, TryOnEngine
from .schemas import Garment

log = logging.getLogger("fitter.router")


class CircuitBreaker:
    """Closed: requests flow. After `threshold` failures in a row it opens for
    `cooldown_s` and requests are skipped. Once the cooldown has passed, one
    probe request is let through (half-open); its success closes the breaker,
    its failure opens it for another cooldown. A success at any point resets
    the failure streak."""

    def __init__(self, threshold: int, cooldown_s: float, clock: Callable[[], float] = time.monotonic) -> None:
        if threshold < 1:
            raise ValueError("threshold must be at least 1")
        if cooldown_s < 0:
            raise ValueError("cooldown_s must not be negative")
        self.threshold = threshold
        self.cooldown_s = cooldown_s
        self._clock = clock
        self._lock = threading.Lock()
        self.failures = 0
        self._open_until: Optional[float] = None
        self._probing = False

    @property
    def is_open(self) -> bool:
        with self._lock:
            return self._open_until is not None

    def allow(self) -> bool:
        """May a request go through right now? Claims the probe slot if so."""
        with self._lock:
            if self._open_until is None:
                return True
            if self._probing or self._clock() < self._open_until:
                return False
            self._probing = True
            return True

    def record_success(self) -> None:
        with self._lock:
            self.failures = 0
            self._open_until = None
            self._probing = False

    def record_failure(self) -> bool:
        """Returns True when this failure opened (or re-opened) the breaker."""
        with self._lock:
            self.failures += 1
            self._probing = False
            if self.failures >= self.threshold:
                self._open_until = self._clock() + self.cooldown_s
                return True
            return False


class Router:
    """Try the primary, fail over to the fallback when the error says another
    engine might help (EngineError.failover). Never fails over after an
    EngineTimeout: the user has already waited the full timeout. Bad-input
    errors are raised as they are; no engine would accept the inputs.

    Each engine has a CircuitBreaker. Engine-side failures (failover=True,
    including timeouts) count towards it; bad-input errors do not, because the
    engine answered properly.
    """

    name = "router"

    def __init__(
        self,
        primary: TryOnEngine,
        fallback: TryOnEngine,
        *,
        failure_threshold: int = 3,
        cooldown_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if primary.name == fallback.name:
            raise ValueError(f"primary and fallback must have different names, both are {primary.name!r}")
        self.engines: tuple[TryOnEngine, ...] = (primary, fallback)
        self.breakers: dict[str, CircuitBreaker] = {
            engine.name: CircuitBreaker(failure_threshold, cooldown_s, clock) for engine in self.engines
        }

    def status(self) -> dict[str, dict]:
        """Per-engine breaker state, for a health endpoint or a log line."""
        return {name: {"failures": b.failures, "open": b.is_open} for name, b in self.breakers.items()}

    def generate(self, selfie: str, garment: Garment) -> EngineResult:
        failures: list[tuple[str, EngineError]] = []
        skipped: list[str] = []
        for engine in self.engines:
            breaker = self.breakers[engine.name]
            if not breaker.allow():
                skipped.append(engine.name)
                continue
            try:
                result = engine.generate(selfie, garment)
            except EngineError as exc:
                if not exc.failover:
                    # The engine is fine; the inputs are not. Counts as a healthy answer.
                    breaker.record_success()
                    raise
                if breaker.record_failure():
                    log.warning("engine=%s circuit opened after %d failures in a row (last: %s)",
                                engine.name, breaker.failures, exc)
                if isinstance(exc, EngineTimeout):
                    raise
                failures.append((engine.name, exc))
                log.warning("engine=%s failed, failing over: %s", engine.name, exc)
                continue
            except Exception:
                # A bug in the adapter, not a vendor answer. Count it, don't mask it.
                breaker.record_failure()
                raise
            if breaker.is_open:
                log.info("engine=%s probe succeeded, circuit closed", engine.name)
            breaker.record_success()
            return result

        if failures:
            detail = "; ".join(f"{name}: {exc}" for name, exc in failures)
            if skipped:
                detail += f" (skipped, cooling down: {', '.join(skipped)})"
            raise EngineError(
                f"all engines failed: {detail}",
                retryable=any(exc.retryable for _, exc in failures),
                failover=any(exc.failover for _, exc in failures),
            )
        raise EngineError(f"no engine available, cooling down: {', '.join(skipped)}", retryable=True)


def build_engine(settings: Settings) -> TryOnEngine:
    """The engine FITTER_ENGINE asks for. Raises ConfigError if a key it needs
    is missing, so a bad deployment fails at startup rather than on the first
    try-on."""
    choice = settings.engine
    if choice not in ENGINES:
        raise ConfigError(f"FITTER_ENGINE must be one of {', '.join(ENGINES)} (got {choice!r})")
    needed = {
        "fake": (),
        "fashn": ("FITTER_FASHN_API_KEY",),
        "fal": ("FITTER_FAL_API_KEY",),
        "router": ("FITTER_FASHN_API_KEY", "FITTER_FAL_API_KEY"),
    }[choice]
    keys = {"FITTER_FASHN_API_KEY": settings.fashn_api_key, "FITTER_FAL_API_KEY": settings.fal_api_key}
    missing = [name for name in needed if not keys[name]]
    if missing:
        raise ConfigError(f"FITTER_ENGINE={choice} needs {' and '.join(missing)} to be set")

    if choice == "fake":
        return FakeEngine()
    if choice == "fashn":
        return FashnEngine(settings.fashn_api_key)
    if choice == "fal":
        return FalEngine(settings.fal_api_key)
    return Router(
        FashnEngine(settings.fashn_api_key),
        FalEngine(settings.fal_api_key),
        failure_threshold=settings.engine_failure_threshold,
        cooldown_s=settings.engine_cooldown_s,
    )
