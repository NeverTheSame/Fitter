"""Background worker: one thread pulling job ids off an in-process queue."""
from __future__ import annotations

import logging
import queue
import threading

from .pipeline import Pipeline

log = logging.getLogger("fitter.worker")

_STOP = object()


class Worker:
    def __init__(self, pipeline: Pipeline) -> None:
        self.pipeline = pipeline
        self._queue: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._loop, name="fitter-worker", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._queue.put(_STOP)
        self._thread.join(timeout)

    def submit(self, job_id: str) -> None:
        self._queue.put(job_id)

    def join_idle(self) -> None:
        """Block until every submitted job has been processed (used by tests)."""
        self._queue.join()

    def _loop(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is _STOP:
                    return
                self.pipeline.run(item)
            except Exception:
                log.exception("job=%s crashed the pipeline", item)
            finally:
                self._queue.task_done()
