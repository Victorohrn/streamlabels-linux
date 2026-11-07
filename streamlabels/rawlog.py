"""Append-only JSON-lines log of raw events, for debugging what a source really sent."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)


class RawLog:
    def __init__(self, path: Path, max_bytes: int = 5_000_000):
        self.path = Path(path)
        self.max_bytes = max_bytes
        self._lock = threading.Lock()

    def write(self, source: str, payload) -> None:
        line = json.dumps({"ts": round(time.time(), 3), "source": source, "payload": payload},
                          ensure_ascii=False, default=str)
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self.path.exists() and self.path.stat().st_size > self.max_bytes:
                    os.replace(self.path, self.path.with_suffix(self.path.suffix + ".1"))
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError as exc:
                log.debug("raw log write failed: %s", exc)
