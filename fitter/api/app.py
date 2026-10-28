"""HTTP API on the standard library's http.server.

Routes:
  GET  /                         web client
  GET  /api/healthz
  GET  /api/me                   free try-ons left
  POST /api/garments/from-url    {"url": ...} -> Garment
  POST /api/tryons               {"selfie": ..., "garment": ...} -> 202 {"id": ...}
  GET  /api/tryons/{id}          job status (the client polls this)
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ..config import Settings
from ..costs import CostLog
from ..credits import FreeTierLedger
from ..engines import TryOnEngine
from ..garments import garment_from_url
from ..router import Router, build_engine
from ..schemas import TryOnJob, ValidationError, parse_tryon_request
from ..store import InMemoryJobStore
from ..worker.pipeline import Pipeline
from ..worker.runner import Worker

log = logging.getLogger("fitter.api")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


class HttpError(Exception):
    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class App:
    settings: Settings
    store: InMemoryJobStore
    ledger: FreeTierLedger
    costs: CostLog
    worker: Worker
    engine: TryOnEngine


def build_app(settings: Settings, engine: TryOnEngine | None = None) -> App:
    """Raises ConfigError if settings.engine needs a key that is not set."""
    engine = engine or build_engine(settings)
    store = InMemoryJobStore()
    ledger = FreeTierLedger(settings.free_tryons)
    costs = CostLog()
    pipeline = Pipeline(engine=engine, store=store, costs=costs, ledger=ledger, step_delay_s=settings.step_delay_s)
    return App(settings, store, ledger, costs, Worker(pipeline), engine)


def make_handler(app: App) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "fitter/0.1"

        # --- plumbing -------------------------------------------------------

        def log_message(self, fmt: str, *args) -> None:
            log.debug("%s %s", self.address_string(), fmt % args)

        def _user_id(self) -> str:
            return self.headers.get("X-Fitter-User", "anonymous")

        def _read_json(self) -> object:
            length = int(self.headers.get("Content-Length") or 0)
            if length > app.settings.max_body_bytes:
                raise HttpError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "body too large")
            raw = self.rfile.read(length) if length else b""
            if not raw:
                return {}
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                raise HttpError(HTTPStatus.BAD_REQUEST, "body is not valid JSON")

        def _send_json(self, status: HTTPStatus, body: object) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _send_file(self, path: Path, content_type: str) -> None:
            data = path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _dispatch(self, routes: dict) -> None:
            path = self.path.split("?", 1)[0]
            try:
                for pattern, fn in routes.items():
                    m = re.fullmatch(pattern, path)
                    if m:
                        return fn(*m.groups())
                raise HttpError(HTTPStatus.NOT_FOUND, "not found")
            except HttpError as e:
                self._send_json(e.status, {"error": str(e)})
            except ValidationError as e:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(e)})
            except Exception:
                log.exception("unhandled error on %s %s", self.command, path)
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal error"})

        def do_GET(self) -> None:
            self._dispatch({
                r"/": self.index,
                r"/api/healthz": self.healthz,
                r"/api/me": self.me,
                r"/api/tryons/([0-9a-f-]{36})": self.get_tryon,
            })

        def do_POST(self) -> None:
            self._dispatch({
                r"/api/garments/from-url": self.post_garment_from_url,
                r"/api/tryons": self.post_tryon,
            })

        # --- routes ---------------------------------------------------------

        def index(self) -> None:
            self._send_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")

        def healthz(self) -> None:
            body: dict = {"ok": True, "engine": app.engine.name}
            if isinstance(app.engine, Router):
                body["engines"] = app.engine.status()
            self._send_json(HTTPStatus.OK, body)

        def me(self) -> None:
            user = self._user_id()
            self._send_json(HTTPStatus.OK, {"user_id": user, "free_left": app.ledger.remaining(user)})

        def post_garment_from_url(self) -> None:
            body = self._read_json()
            url = body.get("url") if isinstance(body, dict) else None
            self._send_json(HTTPStatus.OK, garment_from_url(url).to_dict())

        def post_tryon(self) -> None:
            selfie, garment = parse_tryon_request(self._read_json(), app.settings.max_image_bytes)
            user = self._user_id()
            if not app.ledger.try_consume(user):
                raise HttpError(HTTPStatus.PAYMENT_REQUIRED, "Out of free try-ons.")
            job = app.store.add(TryOnJob(user_id=user, selfie=selfie, garment=garment))
            app.worker.submit(job.id)
            self._send_json(HTTPStatus.ACCEPTED, {"id": job.id})

        def get_tryon(self, job_id: str) -> None:
            job = app.store.get(job_id)
            if job is None:
                raise HttpError(HTTPStatus.NOT_FOUND, "not found")
            self._send_json(HTTPStatus.OK, job.public())

    return Handler


def make_server(app: App) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((app.settings.host, app.settings.port), make_handler(app))
