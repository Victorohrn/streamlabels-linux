"""Atomic text-file output for OBS text sources."""

from __future__ import annotations

import logging
import os
import re
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

_SAFE = re.compile(r"[^A-Za-z0-9_.-]")


def safe_filename(key: str) -> str:
    return _SAFE.sub("_", key).strip("._") or "label"


def normalize_text(value: object) -> str:
    """Streamlabs sends literal backslash-n for line breaks in list labels (see
    streamlabels-manager.ts in Streamlabs Desktop); turn them into real newlines."""
    text = "" if value is None else str(value)
    return text.replace("\\n", "\n")


class LabelWriter:
    def __init__(self, out_dir: Path):
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._last: dict[str, str] = {}

    def path(self, key: str) -> Path:
        return self.out_dir / f"{safe_filename(key)}.txt"

    def write(self, key: str, value: object) -> bool:
        text = normalize_text(value)
        path = self.path(key)
        if self._last.get(key) == text and path.exists():
            return False
        fd, tmp = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".tmp", dir=self.out_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.chmod(tmp, 0o644)
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        self._last[key] = text
        log.debug("wrote %s = %r", path.name, text)
        return True

    def write_all(self, labels: dict[str, object]) -> int:
        n = 0
        for key, value in labels.items():
            if self.write(key, value):
                n += 1
        return n
