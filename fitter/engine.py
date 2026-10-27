"""Try-on engines.

Only FakeEngine exists. It returns no image, and the web client composites the
garment over the selfie itself. The plan's real engines (FASHN primary, fal
fallback) are not wired in yet.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol

from .schemas import Garment


class EngineError(RuntimeError):
    pass


@dataclass
class EngineResult:
    image: Optional[str]  # data URL or http(s) URL of the render; None = client composites
    cost_usd: float


class TryOnEngine(Protocol):
    name: str

    def generate(self, selfie: str, garment: Garment) -> EngineResult: ...


class FakeEngine:
    name = "fake"

    def __init__(self, cost_usd: float = 0.075) -> None:
        # Charged as if it were FASHN v1.6 on demand, so cost reports look realistic.
        self.cost_usd = cost_usd

    def generate(self, selfie: str, garment: Garment) -> EngineResult:
        return EngineResult(image=None, cost_usd=self.cost_usd)
