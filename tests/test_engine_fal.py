"""FalEngine against a scripted local stand-in for queue.fal.run."""
import json
import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from fitter.engines import EngineError, EngineTimeout, FalEngine
from fitter.schemas import Category, Garment
from tests.helpers import PNG_1PX

APP = "fal-ai/image-apps-v2/virtual-try-on"
SUBMIT_PATH = f"/{APP}"
# fal keys requests by owner/app and drops the sub-path.
REQ_PATH = "/fal-ai/image-apps-v2/requests/req_1"
STATUS_PATH = REQ_PATH + "/status"
CANCEL_PATH = REQ_PATH + "/cancel"
OUT_URL = "https://v3.fal.media/files/out.png"


def status(name, **extra):
    return 200, {"status": name, "response_url": "{base}" + REQ_PATH, **extra}


class MockFal:
    """Per-test script. "{base}" in any string is replaced with the mock's URL.

    The last status reply repeats forever. A reply is (http_status, json_body)
    or (http_status, bytes).
    """

    def __init__(self):
        self.app = APP  # which path accepts submits
        self.submit = (200, {
            "request_id": "req_1",
            "status_url": "{base}" + STATUS_PATH,
            "response_url": "{base}" + REQ_PATH,
            "cancel_url": "{base}" + CANCEL_PATH,
        })
        self.statuses = [status("COMPLETED")]
        self.result = (200, {"image": {"url": OUT_URL, "content_type": "image/png"}})
        self.cancel = (202, {"status": "CANCELLATION_REQUESTED"})
        self.requests = []  # (method, path, headers, parsed body)
        self._lock = threading.Lock()

    def next_status(self):
        with self._lock:
            return self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]

    def calls(self):
        return [(m, p) for m, p, _, _ in self.requests]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _record(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.server.mock.requests.append((self.command, self.path, dict(self.headers), json.loads(raw) if raw else None))

    def _reply(self, status, body):
        if not isinstance(body, bytes):
            host, port = self.server.server_address
            body = json.dumps(body).replace("{base}", f"http://{host}:{port}").encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self._record()
        self._reply(*(self.server.mock.submit if self.path == f"/{self.server.mock.app}" else (404, {"detail": "Not Found"})))

    def do_GET(self):
        self._record()
        mock = self.server.mock
        if self.path.endswith("/requests/req_1/status"):
            self._reply(*mock.next_status())
        elif self.path.endswith("/requests/req_1"):
            self._reply(*mock.result)
        else:
            self._reply(404, {"detail": "Not Found"})

    def do_PUT(self):
        self._record()
        self._reply(*(self.server.mock.cancel if self.path.endswith("/requests/req_1/cancel") else (404, {"detail": "Not Found"})))


class FalEngineTest(unittest.TestCase):
    def setUp(self):
        self.mock = MockFal()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.mock = self.mock
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.garment = Garment(image=PNG_1PX)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def engine(self, **kw):
        kw.setdefault("timeout_s", 2.0)
        kw.setdefault("poll_interval_s", 0.01)
        return FalEngine("test-key", base_url=self.base_url, **kw)

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

    def test_submits_polls_then_fetches_result(self):
        self.mock.statuses = [status("IN_QUEUE", queue_position=2), status("IN_PROGRESS"), status("COMPLETED")]
        result = self.generate(cost_usd=0.04)

        self.assertEqual(result.image, OUT_URL)
        self.assertAlmostEqual(result.cost_usd, 0.04)
        self.assertEqual(result.engine, "fal")
        self.assertEqual(self.mock.calls(), [
            ("POST", SUBMIT_PATH),
            ("GET", STATUS_PATH), ("GET", STATUS_PATH), ("GET", STATUS_PATH),
            ("GET", REQ_PATH),
        ])
        for _, _, headers, _ in self.mock.requests:
            self.assertEqual(headers["Authorization"], "Key test-key")
        _, _, headers, body = self.mock.requests[0]
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(body, {
            "person_image_url": PNG_1PX,
            "clothing_image_url": PNG_1PX,
            "preserve_pose": True,
        })

    def test_inputs_url_garment_preserve_pose_and_extra_inputs(self):
        # The app has no category knob, so the garment's category is not sent.
        self.garment = Garment(image="https://shop.example/dress.jpg", category=Category.ONE_PIECES)
        self.generate(preserve_pose=False, extra_inputs={"seed": 7})
        body = self.mock.requests[0][3]
        self.assertEqual(body, {
            "person_image_url": PNG_1PX,
            "clothing_image_url": "https://shop.example/dress.jpg",
            "preserve_pose": False,
            "seed": 7,
        })

    def test_other_app_id_drops_subpath_from_request_urls(self):
        # The plan's same-model failover: FASHN v1.6 hosted on fal. Submit goes
        # to the full app path; status/result are keyed by owner/app only.
        self.mock.app = "fal-ai/fashn/tryon/v1.6"
        self.mock.submit = (200, {"request_id": "req_1"})
        self.assertEqual(self.generate(app="fal-ai/fashn/tryon/v1.6").image, OUT_URL)
        self.assertEqual(self.mock.calls(), [
            ("POST", "/fal-ai/fashn/tryon/v1.6"),
            ("GET", "/fal-ai/fashn/requests/req_1/status"),
            ("GET", "/fal-ai/fashn/requests/req_1"),
        ])

    def test_images_list_output_shape(self):
        self.mock.result = (200, {"images": [{"url": OUT_URL}], "seed": 1})
        self.assertEqual(self.generate().image, OUT_URL)

    def test_plain_string_image_output_shape(self):
        self.mock.result = (200, {"image": OUT_URL})
        self.assertEqual(self.generate().image, OUT_URL)

    # --- request URLs ---------------------------------------------------------

    def test_builds_urls_when_submit_response_has_none(self):
        self.mock.submit = (200, {"request_id": "req_1"})
        self.assertEqual(self.generate().image, OUT_URL)
        self.assertEqual(self.mock.calls(), [("POST", SUBMIT_PATH), ("GET", STATUS_PATH), ("GET", REQ_PATH)])

    def test_ignores_returned_urls_on_another_host(self):
        # The API key rides on every request; never follow a URL off our queue host.
        self.mock.submit = (200, {
            "request_id": "req_1",
            "status_url": "https://evil.example" + STATUS_PATH,
            "response_url": "https://evil.example" + REQ_PATH,
            "cancel_url": "https://evil.example" + CANCEL_PATH,
        })
        self.assertEqual(self.generate().image, OUT_URL)
        self.assertEqual(self.mock.calls(), [("POST", SUBMIT_PATH), ("GET", STATUS_PATH), ("GET", REQ_PATH)])

    # --- failed requests --------------------------------------------------------

    def test_422_on_result_is_an_input_error(self):
        # fal reports COMPLETED for a failed request; the error is on the response fetch.
        self.mock.result = (422, {"detail": [
            {"loc": ["body", "person_image_url"], "msg": "No person detected in the image", "type": "value_error"},
        ]})
        self.assertFails(retryable=False, failover=False, contains="request req_1 failed")
        self.assertFails(retryable=False, failover=False, contains="HTTP 422: No person detected in the image")

    def test_422_with_string_detail(self):
        self.mock.result = (422, {"detail": "Content flagged by moderation"})
        self.assertFails(retryable=False, contains="Content flagged by moderation")

    def test_500_on_result_is_retryable(self):
        self.mock.result = (500, {"detail": "Internal Server Error"})
        self.assertFails(retryable=True, contains="HTTP 500")

    def test_result_without_image(self):
        self.mock.result = (200, {"images": []})
        self.assertFails(retryable=True, contains="without an output image")

    def test_unknown_status(self):
        self.mock.statuses = [status("FAILED")]
        self.assertFails(retryable=True, contains="unknown status 'FAILED'")

    # --- timeout + cancel ---------------------------------------------------------

    def test_times_out_and_cancels(self):
        self.mock.statuses = [status("IN_PROGRESS")]
        with self.assertRaises(EngineTimeout) as ctx:
            self.generate(timeout_s=0.1, poll_interval_s=0.02)
        self.assertTrue(ctx.exception.retryable)
        self.assertTrue(ctx.exception.failover)
        self.assertIn("request req_1 not finished after 0.1s", str(ctx.exception))
        # Cancelled while queued: assumed not billed.
        self.assertEqual((ctx.exception.estimated_cost_usd, ctx.exception.engine), (0.0, "fal"))
        calls = self.mock.calls()
        self.assertEqual(calls[-1], ("PUT", CANCEL_PATH))
        polls = calls.count(("GET", STATUS_PATH))
        self.assertGreaterEqual(polls, 2)
        self.assertLessEqual(polls, 10)
        self.assertNotIn(("GET", REQ_PATH), calls)

    def test_failed_cancel_does_not_mask_the_timeout(self):
        self.mock.statuses = [status("IN_PROGRESS")]
        self.mock.cancel = (500, {"detail": "nope"})
        with self.assertLogs("fitter.engines", level="WARNING") as logs:
            with self.assertRaises(EngineTimeout) as ctx:
                self.generate(timeout_s=0.1, poll_interval_s=0.02, cost_usd=0.04)
        self.assertTrue(any("cancel failed" in line for line in logs.output))
        # Could not cancel, so assume the render ran and was billed: an estimate.
        self.assertEqual((ctx.exception.estimated_cost_usd, ctx.exception.engine), (0.04, "fal"))

    # --- HTTP errors on submit ------------------------------------------------------

    def test_401_means_try_another_engine_not_this_one(self):
        self.mock.submit = (401, {"detail": "Invalid authorization key"})
        self.assertFails(retryable=False, failover=True, contains="HTTP 401: Invalid authorization key")
        self.assertEqual(self.mock.calls(), [("POST", SUBMIT_PATH)])

    def test_402_exhausted_balance_means_try_another_engine(self):
        self.mock.submit = (402, {"detail": "Exhausted balance"})
        self.assertFails(retryable=False, failover=True, contains="Exhausted balance")

    def test_422_on_submit_is_a_bad_input_for_every_engine(self):
        self.mock.submit = (422, {"detail": [{"loc": ["body", "clothing_image_url"], "msg": "field required"}]})
        self.assertFails(retryable=False, failover=False, contains="field required")

    def test_429_is_retryable(self):
        self.mock.submit = (429, {"detail": "Too many requests"})
        self.assertFails(retryable=True, contains="HTTP 429")

    def test_503_is_retryable(self):
        self.mock.submit = (503, b"<html>Service Unavailable</html>")
        self.assertFails(retryable=True, contains="HTTP 503")

    def test_submit_without_request_id(self):
        self.mock.submit = (200, {"ok": True})
        self.assertFails(retryable=True, contains="no request_id")

    def test_invalid_json_body(self):
        self.mock.submit = (200, b"not json")
        self.assertFails(retryable=True, contains="invalid JSON")

    # --- HTTP errors while polling ---------------------------------------------------

    def test_transient_status_error_is_retried(self):
        self.mock.statuses = [(503, {"detail": "try later"}), status("COMPLETED")]
        with self.assertLogs("fitter.engines", level="WARNING") as logs:
            self.assertEqual(self.generate().image, OUT_URL)
        self.assertEqual(len(logs.records), 1)
        self.assertIn("HTTP 503", logs.output[0])
        self.assertEqual(self.mock.calls(), [("POST", SUBMIT_PATH), ("GET", STATUS_PATH), ("GET", STATUS_PATH), ("GET", REQ_PATH)])

    def test_status_404_fails_immediately(self):
        self.mock.statuses = [(404, {"detail": "Request not found"})]
        self.assertFails(retryable=False, contains="HTTP 404: Request not found")
        self.assertEqual(self.mock.calls(), [("POST", SUBMIT_PATH), ("GET", STATUS_PATH)])

    # --- network --------------------------------------------------------------

    def test_connection_refused_is_retryable(self):
        with socket.socket() as s:  # grab a port nobody is listening on
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        engine = FalEngine("test-key", base_url=f"http://127.0.0.1:{port}", timeout_s=1, poll_interval_s=0.01)
        with self.assertRaises(EngineError) as ctx:
            engine.generate(PNG_1PX, self.garment)
        self.assertTrue(ctx.exception.retryable)
        self.assertTrue(ctx.exception.failover)
        self.assertIn(f"POST {SUBMIT_PATH} failed", str(ctx.exception))

    def test_requires_api_key(self):
        with self.assertRaises(ValueError):
            FalEngine("", base_url=self.base_url)


if __name__ == "__main__":
    unittest.main()
