"""FashnEngine against a scripted local stand-in for api.fashn.ai."""
import json
import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from fitter.engines import EngineError, FashnEngine
from fitter.schemas import Category, Garment
from tests.helpers import PNG_1PX

OUT_URL = "https://cdn.fashn.ai/out/pred_1.png"


def ok(status, output=None, error=None):
    return 200, {"id": "pred_1", "status": status, "output": output, "error": error}


class MockFashn:
    """Per-test script: one /run reply and a queue of /status replies.

    The last status reply repeats forever, so a test can model a prediction that
    never finishes. A reply is (http_status, json_body) or (http_status, bytes).
    """

    def __init__(self):
        self.run = (200, {"id": "pred_1", "error": None})
        self.statuses = [ok("completed", [OUT_URL])]
        self.requests = []  # (method, path, headers, parsed body)
        self._lock = threading.Lock()

    def next_status(self):
        with self._lock:
            return self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]

    def paths(self):
        return [p for _, p, _, _ in self.requests]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _record(self, body):
        self.server.mock.requests.append((self.command, self.path, dict(self.headers), body))

    def _reply(self, status, body):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self._record(json.loads(raw) if raw else None)
        if self.path == "/v1/run":
            self._reply(*self.server.mock.run)
        else:
            self._reply(404, {"error": "not found"})

    def do_GET(self):
        self._record(None)
        if self.path.startswith("/v1/status/"):
            self._reply(*self.server.mock.next_status())
        else:
            self._reply(404, {"error": "not found"})


class FashnEngineTest(unittest.TestCase):
    def setUp(self):
        self.mock = MockFashn()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.mock = self.mock
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        self.garment = Garment(image=PNG_1PX)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def engine(self, **kw):
        kw.setdefault("timeout_s", 2.0)
        kw.setdefault("poll_interval_s", 0.01)
        return FashnEngine("test-key", base_url=self.base_url, **kw)

    def generate(self, **kw):
        return self.engine(**kw).generate(PNG_1PX, self.garment)

    def assertFails(self, retryable, contains=None, failover=None, **kw):
        """failover defaults to retryable; pass failover=True for account errors."""
        with self.assertRaises(EngineError) as ctx:
            self.generate(**kw)
        exc = ctx.exception
        self.assertEqual((exc.retryable, exc.failover), (retryable, retryable if failover is None else failover), str(exc))
        if contains:
            self.assertIn(contains, str(ctx.exception))
        return ctx.exception

    # --- happy path ---------------------------------------------------------

    def test_submits_then_polls_until_completed(self):
        self.mock.statuses = [ok("starting"), ok("in_queue"), ok("processing"), ok("completed", [OUT_URL])]
        result = self.generate(cost_usd=0.075)

        self.assertEqual(result.image, OUT_URL)
        self.assertAlmostEqual(result.cost_usd, 0.075)
        self.assertEqual(result.engine, "fashn")
        self.assertEqual(self.mock.paths(), ["/v1/run"] + ["/v1/status/pred_1"] * 4)

        method, _, headers, body = self.mock.requests[0]
        self.assertEqual(method, "POST")
        self.assertEqual(headers["Authorization"], "Bearer test-key")
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(body["model_name"], "tryon-v1.6")
        self.assertEqual(body["inputs"], {
            "model_image": PNG_1PX,
            "garment_image": PNG_1PX,
            "category": "auto",
            "garment_photo_type": "auto",
            "mode": "balanced",
            "num_samples": 1,
        })
        self.assertEqual(self.mock.requests[1][2]["Authorization"], "Bearer test-key")

    def test_passes_category_model_mode_and_extra_inputs(self):
        self.garment = Garment(image="https://shop.example/dress.jpg", category=Category.ONE_PIECES)
        self.generate(model_name="tryon-v1.5", mode="quality", extra_inputs={"seed": 7, "garment_photo_type": "flat-lay"})
        inputs = self.mock.requests[0][3]["inputs"]
        self.assertEqual(inputs["garment_image"], "https://shop.example/dress.jpg")
        self.assertEqual(inputs["category"], "one-pieces")
        self.assertEqual(inputs["mode"], "quality")
        self.assertEqual(inputs["seed"], 7)
        self.assertEqual(inputs["garment_photo_type"], "flat-lay")  # extra_inputs override defaults
        self.assertEqual(self.mock.requests[0][3]["model_name"], "tryon-v1.5")

    def test_base64_output_is_passed_through(self):
        self.mock.statuses = [ok("completed", [PNG_1PX])]
        self.assertEqual(self.generate().image, PNG_1PX)

    # --- failed predictions ---------------------------------------------------

    def test_failed_prediction_on_inputs_is_not_retryable_anywhere(self):
        self.mock.statuses = [ok("processing"), ok("failed", error={"name": "PoseError", "message": "No person detected"})]
        self.assertFails(retryable=False, failover=False, contains="PoseError: No person detected")

    def test_content_moderation_is_not_retryable_anywhere(self):
        self.mock.statuses = [ok("failed", error={"name": "ContentModerationError", "message": "Flagged"})]
        self.assertFails(retryable=False, failover=False, contains="ContentModerationError")

    def test_failed_prediction_on_fashn_side_is_retryable(self):
        self.mock.statuses = [ok("failed", error={"name": "PipelineError", "message": "worker crashed"})]
        self.assertFails(retryable=True, contains="PipelineError")

    def test_failed_prediction_with_string_error(self):
        self.mock.statuses = [ok("failed", error="something broke")]
        self.assertFails(retryable=False, contains="something broke")

    def test_completed_without_output(self):
        self.mock.statuses = [ok("completed", [])]
        self.assertFails(retryable=True, contains="without an output image")

    def test_unknown_status(self):
        self.mock.statuses = [ok("weird")]
        self.assertFails(retryable=True, contains="unknown status 'weird'")

    # --- timeout --------------------------------------------------------------

    def test_times_out_when_prediction_never_finishes(self):
        self.mock.statuses = [ok("processing")]
        exc = self.assertFails(retryable=True, contains="not finished after 0.1s", timeout_s=0.1, poll_interval_s=0.02)
        # Assumed (not confirmed) to be billed once it completes without us: an estimate.
        self.assertEqual((exc.estimated_cost_usd, exc.engine), (0.075, "fashn"))
        polls = self.mock.paths().count("/v1/status/pred_1")
        self.assertGreaterEqual(polls, 2)
        self.assertLessEqual(polls, 10)  # ~0.1s / 0.02s, with slack; proves it stopped

    # --- HTTP errors on /run --------------------------------------------------

    def test_401_means_try_another_engine_not_this_one(self):
        self.mock.run = (401, {"error": "Unauthorized"})
        self.assertFails(retryable=False, failover=True, contains="HTTP 401: Unauthorized")
        self.assertEqual(self.mock.paths(), ["/v1/run"])

    def test_400_is_a_bad_input_for_every_engine(self):
        self.mock.run = (400, {"error": {"name": "InputValidationError", "message": "bad category"}})
        self.assertFails(retryable=False, failover=False, contains="InputValidationError: bad category")

    def test_402_out_of_credits_means_try_another_engine(self):
        self.mock.run = (402, {"error": {"name": "OutOfCredits", "message": "Insufficient credits"}})
        self.assertFails(retryable=False, failover=True, contains="OutOfCredits")

    def test_403_is_an_account_problem_too(self):
        self.mock.run = (403, {"error": "Forbidden"})
        self.assertFails(retryable=False, failover=True, contains="HTTP 403")

    def test_429_is_retryable(self):
        self.mock.run = (429, {"error": "Rate limit exceeded"})
        exc = self.assertFails(retryable=True, contains="HTTP 429")
        self.assertEqual(exc.estimated_cost_usd, 0.0)  # nothing rendered, nothing billed

    def test_500_is_retryable(self):
        self.mock.run = (500, b"<html>Bad Gateway</html>")
        self.assertFails(retryable=True, contains="HTTP 500")

    def test_run_200_with_error_field(self):
        self.mock.run = (200, {"id": None, "error": "Invalid model_name"})
        self.assertFails(retryable=False, contains="Invalid model_name")

    def test_run_200_without_id(self):
        self.mock.run = (200, {"ok": True})
        self.assertFails(retryable=True, contains="no prediction id")

    def test_invalid_json_body(self):
        self.mock.run = (200, b"not json")
        self.assertFails(retryable=True, contains="invalid JSON")

    # --- HTTP errors while polling --------------------------------------------

    def test_transient_status_error_is_retried(self):
        self.mock.statuses = [(503, {"error": "try later"}), ok("completed", [OUT_URL])]
        with self.assertLogs("fitter.engines", level="WARNING") as logs:
            self.assertEqual(self.generate().image, OUT_URL)
        self.assertEqual(len(logs.records), 1)
        self.assertIn("HTTP 503", logs.output[0])
        self.assertEqual(self.mock.paths(), ["/v1/run", "/v1/status/pred_1", "/v1/status/pred_1"])

    def test_status_errors_stop_at_the_deadline(self):
        self.mock.statuses = [(503, {"error": "try later"})]
        with self.assertLogs("fitter.engines", level="WARNING"):
            self.assertFails(retryable=True, contains="not finished after", timeout_s=0.1, poll_interval_s=0.02)

    def test_status_404_fails_immediately(self):
        self.mock.statuses = [(404, {"error": "Prediction not found"})]
        self.assertFails(retryable=False, contains="HTTP 404: Prediction not found")
        self.assertEqual(self.mock.paths(), ["/v1/run", "/v1/status/pred_1"])

    # --- network --------------------------------------------------------------

    def test_connection_refused_is_retryable(self):
        with socket.socket() as s:  # grab a port nobody is listening on
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        engine = FashnEngine("test-key", base_url=f"http://127.0.0.1:{port}/v1", timeout_s=1, poll_interval_s=0.01)
        with self.assertRaises(EngineError) as ctx:
            engine.generate(PNG_1PX, self.garment)
        self.assertTrue(ctx.exception.retryable)
        self.assertTrue(ctx.exception.failover)
        self.assertIn("POST /v1/run failed", str(ctx.exception))

    def test_requires_api_key(self):
        with self.assertRaises(ValueError):
            FashnEngine("", base_url=self.base_url)

    def test_old_import_path_still_works(self):
        import fitter.engine as old
        import fitter.engines as new
        for name in ("EngineError", "EngineTimeout", "EngineResult", "TryOnEngine",
                     "FakeEngine", "FashnEngine", "FalEngine", "FASHN_BASE_URL", "FAL_QUEUE_URL", "FAL_TRYON_APP"):
            self.assertIs(getattr(old, name), getattr(new, name), name)

    def test_engine_error_is_a_runtime_error(self):
        # The pipeline catches Exception, but keep the old contract explicit.
        self.assertTrue(issubclass(EngineError, RuntimeError))
        self.assertEqual((EngineError("x").retryable, EngineError("x").failover), (False, False))
        # retryable implies failover; failover alone is the account-problem case
        self.assertEqual((EngineError("x", retryable=True).retryable, EngineError("x", retryable=True).failover), (True, True))
        self.assertEqual((EngineError("x", failover=True).retryable, EngineError("x", failover=True).failover), (False, True))


if __name__ == "__main__":
    unittest.main()
