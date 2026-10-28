# Fitter

2D photo virtual try-on: upload a photo of yourself, add a garment (image or product URL), get a render of you wearing it. The full plan is in [docs/build-plan.md](docs/build-plan.md).

This is the Phase 1 skeleton. It runs end to end; by default the try-on engine is a placeholder (see `FITTER_ENGINE` below).

## Run

Python 3.10+, no dependencies.

```bash
python -m fitter            # http://127.0.0.1:3000
python -m unittest -v       # tests
```

Settings come from `FITTER_*` environment variables (see `fitter/config.py`): `FITTER_PORT`, `FITTER_FREE_TRYONS`, `FITTER_STEP_DELAY`, ...

The try-on engine is chosen with `FITTER_ENGINE`:

| `FITTER_ENGINE` | What runs | Needs |
|---|---|---|
| `fake` (default) | no API calls; the browser composites the garment | nothing |
| `fashn` | FASHN Try-On v1.6 | `FITTER_FASHN_API_KEY` |
| `fal` | fal `image-apps-v2/virtual-try-on` | `FITTER_FAL_API_KEY` |
| `router` | FASHN, failing over to fal, with a circuit breaker per engine (`FITTER_ENGINE_FAILURE_THRESHOLD`, default 3; `FITTER_ENGINE_COOLDOWN` seconds, default 60) | both keys |

A missing key stops the server at startup. `GET /api/healthz` reports the engine in use and, for the router, each breaker's state.

## Layout

| Path | What |
|---|---|
| `fitter/api/app.py` | HTTP API (stdlib `http.server`) |
| `fitter/schemas.py` | `TryOnJob`, `Garment`, `CostEvent`, request validation |
| `fitter/worker/pipeline.py` | the five steps: acquire_garment, check_inputs, generate, check_output, persist |
| `fitter/worker/runner.py` | background worker thread and queue |
| `fitter/engines/` | try-on engines: `base.py` (protocol, `EngineResult`, `EngineError`), `fake.py`, `fashn.py`, `fal.py`, `transport.py` (shared HTTP + polling). `fitter/engine.py` is a compatibility alias |
| `fitter/router.py` | `Router` (FASHN primary, fal fallback, circuit breaker per engine) and `build_engine()` from settings |
| `fitter/garments.py` | garment from product URL (placeholder) |
| `fitter/credits.py` | free-tier allowance |
| `fitter/costs.py` | per-step cost events |
| `fitter/store.py` | job store |
| `fitter/static/index.html` | web client |

## Current limits

- Jobs, credits and cost events live in memory and are lost on restart.
- The free-tier counter is one global counter, not per user. A failed try-on gives its credit back (once per job).
- A timed-out render is logged as an *estimated* cost (`CostEvent.estimated`, `CostLog.estimated_usd()`), kept apart from confirmed charges. The rules behind the estimate (FASHN bills a prediction that completes after we stop polling; fal bills unless the cancel was accepted) are assumptions, not verified vendor policy; see the comments in `fitter/engines/fashn.py` and `fal.py`.
- The fal input field names and the FASHN error-name list come from the build plan and memory of the vendor docs, not from a live call; confirm both in the bake-off.
- The web client polls `GET /api/tryons/{id}`.
- With the default `FITTER_ENGINE=fake`, no image is rendered; the browser pastes the garment over the photo.
- Garment-from-URL returns a placeholder image; no page parsing.
- No caching of renders.
