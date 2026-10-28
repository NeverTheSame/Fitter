"""HTTP and polling shared by the hosted engines. Every failure mode becomes an EngineError."""
from __future__ import annotations

import http.client
import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Optional, TypeVar
from urllib.parse import urlsplit

from .base import EngineError, EngineTimeout

log = logging.getLogger("fitter.engines.transport")

T = TypeVar("T")


def poll(
    vendor: str,
    ref: str,
    deadline: float,
    timeout_s: float,
    interval_s: float,
    fetch: Callable[[], dict],
    interpret: Callable[[dict], Optional[T]],
) -> T:
    """Call fetch() every interval_s until interpret() returns a value.

    A transient failure of fetch() (5xx, network) is logged and retried until
    the deadline: a blip on a status endpoint should not lose a paid render.
    Definite answers (4xx) and anything interpret() raises propagate at once.
    """
    while True:
        try:
            body = fetch()
        except EngineError as exc:
            if not exc.retryable:
                raise
            log.warning("%s %s status poll failed, retrying: %s", vendor, ref, exc)
        else:
            result = interpret(body)
            if result is not None:
                return result
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise EngineTimeout(f"{vendor} {ref} not finished after {timeout_s:g}s")
        time.sleep(min(interval_s, remaining))


def http_json(vendor: str, method: str, url: str, headers: dict[str, str],
              payload: Optional[dict], timeout_s: float) -> dict:
    """One JSON round trip."""
    where = f"{vendor} {method} {urlsplit(url).path}"
    headers = {**headers, "Accept": "application/json"}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = error_text(error_from_body(exc.read())) or exc.reason
        retryable = exc.code in (408, 429) or exc.code >= 500
        account = exc.code in (401, 402, 403)  # bad key, out of credits, forbidden: this engine, not the inputs
        raise EngineError(f"{where} -> HTTP {exc.code}: {detail}", retryable=retryable, failover=account) from exc
    except urllib.error.URLError as exc:  # DNS, connection refused, socket timeout
        raise EngineError(f"{where} failed: {exc.reason}", retryable=True) from exc
    except (OSError, http.client.HTTPException) as exc:
        raise EngineError(f"{where} failed: {exc}", retryable=True) from exc
    if not raw.strip():
        return {}
    try:
        body = json.loads(raw)
    except ValueError as exc:
        raise EngineError(f"{where} returned invalid JSON", retryable=True) from exc
    if not isinstance(body, dict):
        raise EngineError(f"{where} returned a non-object body", retryable=True)
    return body


def error_from_body(raw: bytes) -> Any:
    """Pull the error out of an HTTP error body. FASHN uses {"error": ...},
    fal uses {"detail": ...} (a string, or a list of validation problems)."""
    try:
        body = json.loads(raw)
    except ValueError:
        text = raw.decode(errors="replace").strip()
        return text[:200] or None
    if isinstance(body, dict):
        return body.get("error") or body.get("message") or body.get("detail")
    return body


def error_text(err: Any) -> str:
    if err is None:
        return ""
    if isinstance(err, list):
        return "; ".join(filter(None, (error_text(e) for e in err)))
    if isinstance(err, dict):
        name, message = err.get("name"), err.get("message") or err.get("msg")
        if name and message:
            return f"{name}: {message}"
        return str(message or name or err)
    return str(err)
