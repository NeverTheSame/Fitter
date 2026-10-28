"""Placeholder engine: renders nothing, the web client composites the garment itself."""
from __future__ import annotations

from ..schemas import Garment
from .base import EngineResult


class FakeEngine:
    name = "fake"

    def __init__(self, cost_usd: float = 0.075) -> None:
        # Charged as if it were FASHN v1.6 on demand, so cost reports look realistic.
        self.cost_usd = cost_usd

    def generate(self, selfie: str, garment: Garment) -> EngineResult:
        return EngineResult(image=None, cost_usd=self.cost_usd, engine=self.name)
