"""FASHN Try-On, the primary engine: POST /v1/run, poll GET /v1/status/{id}."""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

from ..schemas import Garment
from .base import EngineError, EngineResult, EngineTimeout
from .transport import error_text, http_json, poll

log = logging.getLogger("fitter.engines.fashn")

FASHN_BASE_URL = "https://api.fashn.ai/v1"

# Status values FASHN reports while a prediction is still being worked on.
_PENDING = frozenset({"starting", "in_queue", "processing"})

# Error names FASHN attaches to a failed prediction when the fault is on its
# side. Everything else (ImageLoadError, ContentModerationError, PoseError,
# ...) is about the inputs. Re-check against docs.fashn.ai when the API changes.
_TRANSIENT_ERRORS = frozenset({"PipelineError", "ThirdPartyError", "InternalServerError"})


class FashnEngine:
    """Inputs are passed through as-is: FASHN accepts both base64 data URLs and
    http(s) URLs for model_image and garment_image, and our Category values are
    FASHN's category names. The render comes back as a CDN URL (deleted by
    FASHN after 3 days) unless extra_inputs sets return_base64.
    """

    name = "fashn"

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = FASHN_BASE_URL,
        model_name: str = "tryon-v1.6",
        mode: str = "balanced",  # performance | balanced | quality
        extra_inputs: Optional[dict[str, Any]] = None,  # garment_photo_type, seed, ...
        cost_usd: float = 0.075,
        timeout_s: float = 90.0,  # submit + polling, end to end
        poll_interval_s: float = 1.0,
        request_timeout_s: float = 30.0,  # per HTTP call
    ) -> None:
        if not api_key:
            raise ValueError("FASHN api_key is required")
        self._api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model_name = model_name
        self.mode = mode
        self.extra_inputs = dict(extra_inputs or {})
        self.cost_usd = cost_usd
        self.timeout_s = timeout_s
        self.poll_interval_s = poll_interval_s
        self.request_timeout_s = request_timeout_s

    def generate(self, selfie: str, garment: Garment) -> EngineResult:
        started = time.monotonic()
        deadline = started + self.timeout_s
        prediction_id = self._submit(selfie, garment)
        try:
            image = poll(
                "FASHN", f"prediction {prediction_id}", deadline, self.timeout_s, self.poll_interval_s,
                fetch=lambda: self._request("GET", f"/status/{prediction_id}"),
                interpret=lambda body: self._interpret_status(body, prediction_id),
            )
        except EngineTimeout as exc:
            # ASSUMPTION, not verified against FASHN's docs or the build plan:
            # the API has no cancel call, the prediction keeps running after we
            # stop polling, and FASHN bills it once it completes. So a timeout
            # is treated as probably billed. Recorded as an estimate; confirm
            # against the FASHN dashboard after the first real timeouts.
            exc.estimated_cost_usd, exc.engine = self.cost_usd, self.name
            raise
        log.info("fashn prediction=%s completed in %.1fs", prediction_id, time.monotonic() - started)
        return EngineResult(image=image, cost_usd=self.cost_usd, engine=self.name)

    def _submit(self, selfie: str, garment: Garment) -> str:
        inputs: dict[str, Any] = {
            "model_image": selfie,
            "garment_image": garment.image,
            "category": garment.category.value,
            "garment_photo_type": "auto",
            "mode": self.mode,
            "num_samples": 1,
        }
        inputs.update(self.extra_inputs)
        body = self._request("POST", "/run", {"model_name": self.model_name, "inputs": inputs})
        if body.get("error"):
            raise EngineError(f"FASHN rejected the request: {error_text(body['error'])}")
        prediction_id = body.get("id")
        if not isinstance(prediction_id, str) or not prediction_id:
            raise EngineError("FASHN /run returned no prediction id", retryable=True)
        log.info("fashn prediction=%s submitted (model=%s mode=%s)", prediction_id, self.model_name, self.mode)
        return prediction_id

    def _interpret_status(self, body: dict, prediction_id: str) -> Optional[str]:
        """The render URL once completed, None while pending, EngineError on failure."""
        status = body.get("status")
        if status == "completed":
            output = body.get("output")
            if isinstance(output, list) and output and isinstance(output[0], str) and output[0]:
                return output[0]
            raise EngineError(f"FASHN prediction {prediction_id} completed without an output image", retryable=True)
        if status == "failed":
            err = body.get("error")
            name = err.get("name") if isinstance(err, dict) else None
            raise EngineError(
                f"FASHN prediction {prediction_id} failed: {error_text(err)}",
                retryable=name in _TRANSIENT_ERRORS,
            )
        if status not in _PENDING:
            raise EngineError(f"FASHN prediction {prediction_id} returned unknown status {status!r}", retryable=True)
        return None

    def _request(self, method: str, path: str, payload: Optional[dict] = None) -> dict:
        return http_json("FASHN", method, self.base_url + path,
                         {"Authorization": f"Bearer {self._api_key}"}, payload, self.request_timeout_s)
