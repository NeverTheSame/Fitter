"""Run the server: python -m fitter"""
from __future__ import annotations

import logging
import sys

from .api.app import build_app, make_server
from .config import ConfigError, Settings


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    settings = Settings.from_env()
    try:
        app = build_app(settings)
    except ConfigError as exc:
        sys.exit(f"fitter: {exc}")
    app.worker.start()
    server = make_server(app)
    logging.getLogger("fitter").info("engine=%s listening on http://%s:%d", app.engine.name, settings.host, settings.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        app.worker.stop()


if __name__ == "__main__":
    main()
