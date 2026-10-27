# Fitter

2D photo virtual try-on: upload a photo of yourself, add a garment (image or product URL), get a render of you wearing it. The full plan is in [docs/build-plan.md](docs/build-plan.md).

This is the Phase 1 skeleton. It runs end to end but the try-on engine is a placeholder.

## Run

Python 3.10+, no dependencies.

```bash
python -m fitter            # http://127.0.0.1:3000
python -m unittest -v       # tests
```

Settings come from `FITTER_*` environment variables (see `fitter/config.py`): `FITTER_PORT`, `FITTER_FREE_TRYONS`, `FITTER_STEP_DELAY`, ...

## Layout

| Path | What |
|---|---|
| `fitter/api/app.py` | HTTP API (stdlib `http.server`) |
| `fitter/schemas.py` | `TryOnJob`, `Garment`, `CostEvent`, request validation |
| `fitter/worker/pipeline.py` | the five steps: acquire_garment, check_inputs, generate, check_output, persist |
| `fitter/worker/runner.py` | background worker thread and queue |
| `fitter/engine.py` | engine interface + `FakeEngine` |
| `fitter/garments.py` | garment from product URL (placeholder) |
| `fitter/credits.py` | free-tier allowance |
| `fitter/costs.py` | per-step cost events |
| `fitter/store.py` | job store |
| `fitter/static/index.html` | web client |

## Current limits

- Jobs, credits and cost events live in memory and are lost on restart.
- The free-tier counter is one global counter, not per user.
- The web client polls `GET /api/tryons/{id}`.
- `FakeEngine` returns no image; the browser pastes the garment over the photo.
- Garment-from-URL returns a placeholder image; no page parsing.
- No caching of renders.
