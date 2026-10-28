"""What every try-on engine shares: the protocol, the result, the errors."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol

from ..schemas import Garment


class EngineError(RuntimeError):
    """The engine did not produce a render. Two flags say what might help:

    retryable: the same engine may succeed if asked again. Rate limits, 5xx,
        network errors, invalid responses, timeouts.
    failover:  another engine may succeed. Everything retryable, plus problems
        with this engine's account: bad key (401/403), out of credits (402).

    Both False: the inputs were rejected (400, 422, no person detected,
    content moderation). No engine will take them; don't spend money retrying.
    retryable implies failover.

    estimated_cost_usd is the engine's best guess at what the vendor will bill
    for the failed attempt anyway, and engine names who. It is 0 for nearly
    everything (vendors bill renders, not errors); the engines set it only on
    timeouts, and on rules that are assumptions, not confirmed vendor policy
    (see the comments in fashn.py and fal.py). The ledger records it as an
    estimate, not a confirmed charge.
    """

    def __init__(self, message: str, *, retryable: bool = False, failover: bool = False,
                 estimated_cost_usd: float = 0.0, engine: Optional[str] = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.failover = retryable or failover
        self.estimated_cost_usd = estimated_cost_usd
        self.engine = engine


class EngineTimeout(EngineError):
    """The engine did not finish within timeout_s. Retryable on paper, but the
    user has already waited the full timeout, so a router should not start
    over on another engine."""

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=True)


@dataclass
class EngineResult:
    image: Optional[str]  # data URL or http(s) URL of the render; None = client composites
    cost_usd: float
    engine: Optional[str] = None  # name of the engine that rendered it (matters behind a router)


class TryOnEngine(Protocol):
    name: str

    def generate(self, selfie: str, garment: Garment) -> EngineResult: ...
