"""fal virtual try-on through fal's queue API, the fallback engine."""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

from ..schemas import Garment
from .base import EngineError, EngineResult, EngineTimeout
from .transport import http_json, poll

log = logging.getLogger("fitter.engines.fal")

FAL_QUEUE_URL = "https://queue.fal.run"
FAL_TRYON_APP = "fal-ai/image-apps-v2/virtual-try-on"

_PENDING = frozenset({"IN_QUEUE", "IN_PROGRESS"})


class FalEngine:
    """POST {queue}/{app} submits and returns request_id plus status/response/
    cancel URLs; GET status until COMPLETED; GET the response URL for the
    output. A request that failed inside the app still reports COMPLETED, and
    the failure surfaces as an HTTP error on the response fetch, so that fetch
    is where input errors (422) and fal-side errors (5xx) get told apart.

    The app takes a person image and a clothing image (URL or data URI) and a
    preserve_pose toggle; it has no category knob, so garment.category is
    ignored. Output URLs live on fal's CDN. Input field names follow the
    model page linked from the build plan; confirm them in the bake-off.
    """

    name = "fal"

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = FAL_QUEUE_URL,
        app: str = FAL_TRYON_APP,
        preserve_pose: bool = True,
        extra_inputs: Optional[dict[str, Any]] = None,
        cost_usd: float = 0.04,
        timeout_s: float = 90.0,
        poll_interval_s: float = 1.0,
        request_timeout_s: float = 30.0,
    ) -> None:
        if not api_key:
            raise ValueError("fal api_key is required")
        self._api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.app = app.strip("/")
        self.preserve_pose = preserve_pose
        self.extra_inputs = dict(extra_inputs or {})
        self.cost_usd = cost_usd
        self.timeout_s = timeout_s
        self.poll_interval_s = poll_interval_s
        self.request_timeout_s = request_timeout_s

    def generate(self, selfie: str, garment: Garment) -> EngineResult:
        started = time.monotonic()
        deadline = started + self.timeout_s
        request_id, urls = self._submit(selfie, garment)
        try:
            poll(
                "fal", f"request {request_id}", deadline, self.timeout_s, self.poll_interval_s,
                fetch=lambda: self._request("GET", urls["status"]),
                interpret=lambda body: self._interpret_status(body, request_id),
            )
        except EngineTimeout as exc:
            # ASSUMPTION, not verified against fal's docs or the build plan:
            # a request cancelled while still queued is not billed, and one
            # that was already running (cancel refused) is. So a timeout is
            # treated as billed only when the cancel did not go through.
            # Recorded as an estimate; confirm against fal's usage page.
            exc.engine = self.name
            if not self._cancel(request_id, urls["cancel"]):
                exc.estimated_cost_usd = self.cost_usd
            raise
        image = self._fetch_output(request_id, urls["response"])
        log.info("fal request=%s completed in %.1fs", request_id, time.monotonic() - started)
        return EngineResult(image=image, cost_usd=self.cost_usd, engine=self.name)

    def _submit(self, selfie: str, garment: Garment) -> tuple[str, dict[str, str]]:
        inputs: dict[str, Any] = {
            "person_image_url": selfie,
            "clothing_image_url": garment.image,
            "preserve_pose": self.preserve_pose,
        }
        inputs.update(self.extra_inputs)
        body = self._request("POST", f"{self.base_url}/{self.app}", inputs)
        request_id = body.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            raise EngineError("fal submit returned no request_id", retryable=True)
        log.info("fal request=%s submitted (app=%s)", request_id, self.app)
        return request_id, self._request_urls(request_id, body)

    def _request_urls(self, request_id: str, body: dict) -> dict[str, str]:
        """Status/response/cancel URLs: the ones fal returned, if they are on
        our queue host (the API key goes in every request, so don't follow a
        URL that points elsewhere); otherwise built from the app id. fal keys
        requests by the app's owner/name and drops any sub-path."""
        owner_app = "/".join(self.app.split("/")[:2])
        default = f"{self.base_url}/{owner_app}/requests/{request_id}"
        urls = {"status": f"{default}/status", "response": default, "cancel": f"{default}/cancel"}
        for key in urls:
            given = body.get(f"{key}_url")
            if isinstance(given, str) and given.startswith(self.base_url + "/"):
                urls[key] = given
        return urls

    def _interpret_status(self, body: dict, request_id: str) -> Optional[bool]:
        status = body.get("status")
        if status == "COMPLETED":
            return True
        if status not in _PENDING:
            raise EngineError(f"fal request {request_id} returned unknown status {status!r}", retryable=True)
        return None

    def _fetch_output(self, request_id: str, response_url: str) -> str:
        try:
            body = self._request("GET", response_url)
        except EngineError as exc:
            raise EngineError(f"fal request {request_id} failed: {exc}",
                              retryable=exc.retryable, failover=exc.failover) from exc
        image = _output_url(body)
        if not image:
            raise EngineError(f"fal request {request_id} completed without an output image", retryable=True)
        return image

    def _cancel(self, request_id: str, cancel_url: str) -> bool:
        try:
            self._request("PUT", cancel_url)
        except EngineError as exc:
            log.warning("fal request=%s cancel failed: %s", request_id, exc)
            return False
        log.info("fal request=%s cancelled after timeout", request_id)
        return True

    def _request(self, method: str, url: str, payload: Optional[dict] = None) -> dict:
        return http_json("fal", method, url, {"Authorization": f"Key {self._api_key}"}, payload,
                         self.request_timeout_s)


def _output_url(body: dict) -> Optional[str]:
    """fal image apps return {"image": {"url": ...}} or {"images": [{"url": ...}]}."""
    candidates: list[Any] = [body.get("image")]
    images = body.get("images")
    if isinstance(images, list) and images:
        candidates.append(images[0])
    for item in candidates:
        if isinstance(item, dict):
            item = item.get("url")
        if isinstance(item, str) and item:
            return item
    return None
