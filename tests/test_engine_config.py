"""FITTER_ENGINE selection: settings -> engine, startup failure on a missing key."""
import json
import os
import socket
import subprocess
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from fitter.api.app import build_app, make_server
from fitter.config import ConfigError, Settings
from fitter.engines import FakeEngine, FalEngine, FashnEngine
from fitter.router import Router, build_engine

REPO = Path(__file__).resolve().parent.parent


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class SettingsFromEnvTest(unittest.TestCase):
    def test_defaults_without_env(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            s = Settings.from_env()
        self.assertEqual(s.engine, "fake")
        self.assertEqual((s.fashn_api_key, s.fal_api_key), ("", ""))
        self.assertEqual((s.engine_failure_threshold, s.engine_cooldown_s), (3, 60.0))

    def test_reads_engine_keys_and_breaker_settings(self):
        env = {
            "FITTER_ENGINE": " Router ",
            "FITTER_FASHN_API_KEY": "fashn-secret",
            "FITTER_FAL_API_KEY": "fal-secret",
            "FITTER_ENGINE_FAILURE_THRESHOLD": "5",
            "FITTER_ENGINE_COOLDOWN": "12.5",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            s = Settings.from_env()
        self.assertEqual(s.engine, "router")
        self.assertEqual((s.fashn_api_key, s.fal_api_key), ("fashn-secret", "fal-secret"))
        self.assertEqual((s.engine_failure_threshold, s.engine_cooldown_s), (5, 12.5))


class BuildEngineTest(unittest.TestCase):
    def test_fake_is_the_default_and_needs_no_key(self):
        engine = build_engine(Settings())
        self.assertIsInstance(engine, FakeEngine)

    def test_fashn(self):
        engine = build_engine(Settings(engine="fashn", fashn_api_key="k1"))
        self.assertIsInstance(engine, FashnEngine)
        self.assertEqual(engine._api_key, "k1")

    def test_fal(self):
        engine = build_engine(Settings(engine="fal", fal_api_key="k2"))
        self.assertIsInstance(engine, FalEngine)
        self.assertEqual(engine._api_key, "k2")

    def test_router_with_breaker_settings(self):
        engine = build_engine(Settings(engine="router", fashn_api_key="k1", fal_api_key="k2",
                                       engine_failure_threshold=2, engine_cooldown_s=5.0))
        self.assertIsInstance(engine, Router)
        self.assertEqual([e.name for e in engine.engines], ["fashn", "fal"])
        self.assertIsInstance(engine.engines[0], FashnEngine)
        self.assertIsInstance(engine.engines[1], FalEngine)
        for breaker in engine.breakers.values():
            self.assertEqual((breaker.threshold, breaker.cooldown_s), (2, 5.0))

    def test_fashn_without_key(self):
        with self.assertRaises(ConfigError) as ctx:
            build_engine(Settings(engine="fashn", fal_api_key="irrelevant"))
        self.assertEqual(str(ctx.exception), "FITTER_ENGINE=fashn needs FITTER_FASHN_API_KEY to be set")

    def test_fal_without_key(self):
        with self.assertRaises(ConfigError) as ctx:
            build_engine(Settings(engine="fal", fashn_api_key="irrelevant"))
        self.assertEqual(str(ctx.exception), "FITTER_ENGINE=fal needs FITTER_FAL_API_KEY to be set")

    def test_router_names_every_missing_key(self):
        with self.assertRaises(ConfigError) as ctx:
            build_engine(Settings(engine="router"))
        self.assertEqual(str(ctx.exception),
                         "FITTER_ENGINE=router needs FITTER_FASHN_API_KEY and FITTER_FAL_API_KEY to be set")
        with self.assertRaises(ConfigError) as ctx:
            build_engine(Settings(engine="router", fashn_api_key="k1"))
        self.assertEqual(str(ctx.exception), "FITTER_ENGINE=router needs FITTER_FAL_API_KEY to be set")

    def test_unknown_engine(self):
        with self.assertRaises(ConfigError) as ctx:
            build_engine(Settings(engine="replicate"))
        self.assertIn("must be one of fake, fashn, fal, router (got 'replicate')", str(ctx.exception))


class AppEngineTest(unittest.TestCase):
    """build_app + /api/healthz per engine. No try-on is submitted, so no API is called."""

    def serve(self, settings):
        app = build_app(settings)
        server = make_server(app)
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return app, f"http://127.0.0.1:{server.server_address[1]}"

    def healthz(self, base):
        with urllib.request.urlopen(base + "/api/healthz") as r:
            return json.loads(r.read())

    def test_build_app_fails_early_on_missing_key(self):
        with self.assertRaises(ConfigError):
            build_app(Settings(engine="fashn"))

    def test_healthz_fake(self):
        _, base = self.serve(Settings(port=0))
        self.assertEqual(self.healthz(base), {"ok": True, "engine": "fake"})

    def test_healthz_single_real_engine(self):
        app, base = self.serve(Settings(port=0, engine="fal", fal_api_key="k"))
        self.assertIsInstance(app.engine, FalEngine)
        self.assertEqual(self.healthz(base), {"ok": True, "engine": "fal"})

    def test_healthz_router_shows_breakers(self):
        app, base = self.serve(Settings(port=0, engine="router", fashn_api_key="k1", fal_api_key="k2"))
        self.assertIsInstance(app.engine, Router)
        self.assertEqual(self.healthz(base), {
            "ok": True,
            "engine": "router",
            "engines": {
                "fashn": {"failures": 0, "open": False},
                "fal": {"failures": 0, "open": False},
            },
        })

    def test_explicit_engine_argument_still_wins(self):
        class Custom:
            name = "custom"

            def generate(self, selfie, garment):
                raise AssertionError("not called")

        app = build_app(Settings(engine="fashn"), engine=Custom())  # no key needed: engine is given
        self.assertEqual(app.engine.name, "custom")


class MainProcessTest(unittest.TestCase):
    """`python -m fitter` end to end, in a subprocess, no network beyond localhost."""

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items() if not k.startswith("FITTER_")}
        env["PYTHONPATH"] = str(REPO)
        env.update(extra)
        return env

    def test_missing_key_exits_with_a_clear_message(self):
        proc = subprocess.run([sys.executable, "-m", "fitter"], cwd=REPO, capture_output=True, text=True,
                              timeout=30, env=self.env(FITTER_ENGINE="router", FITTER_FASHN_API_KEY="k1"))
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stderr.strip().splitlines()[-1],
                         "fitter: FITTER_ENGINE=router needs FITTER_FAL_API_KEY to be set")
        self.assertNotIn("Traceback", proc.stderr)

    def test_default_still_starts_with_the_fake_engine(self):
        port = free_port()
        proc = subprocess.Popen([sys.executable, "-m", "fitter"], cwd=REPO, env=self.env(FITTER_PORT=str(port)),
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.time() + 15
            body = None
            while time.time() < deadline and body is None:
                if proc.poll() is not None:
                    self.fail(f"server exited early: {proc.stderr.read()}")
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/healthz", timeout=1) as r:
                        body = json.loads(r.read())
                except (urllib.error.URLError, ConnectionError):
                    time.sleep(0.05)
            self.assertEqual(body, {"ok": True, "engine": "fake"})
        finally:
            proc.terminate()
            proc.wait(timeout=10)
            proc.stderr.close()


if __name__ == "__main__":
    unittest.main()
