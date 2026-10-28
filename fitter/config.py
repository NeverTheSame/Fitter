"""Runtime settings, read from FITTER_* environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw else default


class ConfigError(ValueError):
    """Settings that cannot work. Raised at startup, before any request is served."""


ENGINES = ("fake", "fashn", "fal", "router")


@dataclass(frozen=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 3000
    free_tryons: int = 3
    # Pause between pipeline steps so the UI can show progress. 0 in tests.
    step_delay_s: float = 0.6
    max_image_bytes: int = 8 * 1024 * 1024
    max_body_bytes: int = 24 * 1024 * 1024
    # Try-on engine: fake (no API calls), fashn, fal, or router (fashn with fal as fallback).
    engine: str = "fake"
    fashn_api_key: str = ""
    fal_api_key: str = ""
    # Router circuit breaker: skip an engine for cooldown_s after this many failures in a row.
    engine_failure_threshold: int = 3
    engine_cooldown_s: float = 60.0

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            host=os.environ.get("FITTER_HOST", cls.host),
            port=_env_int("FITTER_PORT", cls.port),
            free_tryons=_env_int("FITTER_FREE_TRYONS", cls.free_tryons),
            step_delay_s=_env_float("FITTER_STEP_DELAY", cls.step_delay_s),
            max_image_bytes=_env_int("FITTER_MAX_IMAGE_BYTES", cls.max_image_bytes),
            max_body_bytes=_env_int("FITTER_MAX_BODY_BYTES", cls.max_body_bytes),
            engine=os.environ.get("FITTER_ENGINE", cls.engine).strip().lower() or cls.engine,
            fashn_api_key=os.environ.get("FITTER_FASHN_API_KEY", "").strip(),
            fal_api_key=os.environ.get("FITTER_FAL_API_KEY", "").strip(),
            engine_failure_threshold=_env_int("FITTER_ENGINE_FAILURE_THRESHOLD", cls.engine_failure_threshold),
            engine_cooldown_s=_env_float("FITTER_ENGINE_COOLDOWN", cls.engine_cooldown_s),
        )
