import json
import logging
import threading
import time
import unittest
import urllib.error
import urllib.request

from fitter.api.app import build_app, make_server
from fitter.config import Settings
from fitter.engines import EngineError, EngineTimeout
from tests.helpers import PNG_1PX


class FailingEngine:
    name = "failing"

    def __init__(self, make_error):
        self.make_error = make_error

    def generate(self, selfie, garment):
        raise self.make_error()


class ApiTestBase(unittest.TestCase):
    engine = None  # None -> the configured (fake) engine

    def setUp(self):
        self.app = build_app(Settings(port=0, step_delay_s=0, free_tryons=2), engine=self.engine)
        self.app.worker.start()
        self.server = make_server(self.app)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.app.worker.stop()

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def wait_done(self, job_id, timeout=5):
        deadline = time.time() + timeout
        while time.time() < deadline:
            _, job = self.call("GET", f"/api/tryons/{job_id}")
            if job["status"] in ("done", "failed"):
                return job
            time.sleep(0.02)
        self.fail("job did not finish")


class ApiTest(ApiTestBase):
    def test_tryon_flow(self):
        status, body = self.call("POST", "/api/tryons", {"selfie": PNG_1PX, "garment": PNG_1PX})
        self.assertEqual(status, 202)
        job = self.wait_done(body["id"])
        self.assertEqual(job["status"], "done")
        self.assertAlmostEqual(job["cost_usd"], 0.075)
        self.assertNotIn("selfie", job)

    def test_free_tier_limit(self):
        for _ in range(2):
            self.assertEqual(self.call("POST", "/api/tryons", {"selfie": PNG_1PX, "garment": PNG_1PX})[0], 202)
        status, body = self.call("POST", "/api/tryons", {"selfie": PNG_1PX, "garment": PNG_1PX})
        self.assertEqual(status, 402)
        self.assertEqual(self.call("GET", "/api/me")[1]["free_left"], 0)

    def test_bad_input_is_400_and_costs_nothing(self):
        status, _ = self.call("POST", "/api/tryons", {"selfie": "nope"})
        self.assertEqual(status, 400)
        self.assertEqual(self.call("GET", "/api/me")[1]["free_left"], 2)

    def test_garment_from_url(self):
        status, g = self.call("POST", "/api/garments/from-url", {"url": "https://shop.example/p/1"})
        self.assertEqual(status, 200)
        self.assertTrue(g["image"].startswith("https://"))
        self.assertEqual(self.call("POST", "/api/garments/from-url", {"url": "ftp://x"})[0], 400)

    def test_unknown_job_is_404(self):
        self.assertEqual(self.call("GET", "/api/tryons/00000000-0000-0000-0000-000000000000")[0], 404)


class FailedTryOnApiTest(ApiTestBase):
    engine = FailingEngine(lambda: EngineError("FASHN POST /v1/run -> HTTP 503: down", retryable=True))

    def test_failed_job_shows_reason_and_gives_the_credit_back(self):
        logging.getLogger("fitter.pipeline").setLevel(logging.CRITICAL)
        self.addCleanup(logging.getLogger("fitter.pipeline").setLevel, logging.NOTSET)
        status, body = self.call("POST", "/api/tryons", {"selfie": PNG_1PX, "garment": PNG_1PX})
        self.assertEqual(status, 202)

        job = self.wait_done(body["id"])
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["error"], "The try-on service is unavailable right now. "
                                       "Please try again in a few minutes. Your try-on credit has been returned.")
        self.assertNotIn("error_detail", job)  # vendor detail is not public
        self.assertEqual(job["cost_usd"], 0)
        self.assertIsNone(job["render"])
        self.assertEqual(self.call("GET", "/api/me")[1]["free_left"], 2)

        # Both free try-ons are still available after the failure.
        self.assertEqual(self.call("POST", "/api/tryons", {"selfie": PNG_1PX, "garment": PNG_1PX})[0], 202)
        self.app.worker.join_idle()
        self.assertEqual(self.call("GET", "/api/me")[1]["free_left"], 2)


class TimedOutTryOnApiTest(ApiTestBase):
    engine = FailingEngine(lambda: EngineTimeout("fal request r1 not finished after 90s"))

    def test_timed_out_job_shows_reason_and_gives_the_credit_back(self):
        logging.getLogger("fitter.pipeline").setLevel(logging.CRITICAL)
        self.addCleanup(logging.getLogger("fitter.pipeline").setLevel, logging.NOTSET)
        _, body = self.call("POST", "/api/tryons", {"selfie": PNG_1PX, "garment": PNG_1PX})
        job = self.wait_done(body["id"])
        self.assertEqual(job["status"], "failed")
        self.assertTrue(job["error"].startswith("The try-on took too long and was cancelled."))
        self.assertIn("credit has been returned", job["error"])
        self.assertEqual(self.call("GET", "/api/me")[1]["free_left"], 2)


if __name__ == "__main__":
    unittest.main()
