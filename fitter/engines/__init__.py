"""Try-on engines.

FakeEngine renders nothing (the web client composites the garment itself),
FashnEngine is the primary hosted engine, FalEngine the fallback. The router
that picks between them is fitter.router; FITTER_ENGINE chooses what the app
builds (see fitter.config).
"""
from .base import EngineError, EngineResult, EngineTimeout, TryOnEngine
from .fake import FakeEngine
from .fal import FAL_QUEUE_URL, FAL_TRYON_APP, FalEngine
from .fashn import FASHN_BASE_URL, FashnEngine

__all__ = [
    "EngineError", "EngineResult", "EngineTimeout", "TryOnEngine",
    "FakeEngine", "FashnEngine", "FalEngine",
    "FASHN_BASE_URL", "FAL_QUEUE_URL", "FAL_TRYON_APP",
]
