"""Best-effort file cache for API pages. Any cache failure is ignored, never fatal."""

import hashlib
import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger(__name__)


class FileCache:
    def __init__(self, directory: str, ttl_seconds: int):
        self.dir = Path(directory)
        self.ttl = ttl_seconds

    def _path(self, params: dict[str, str]) -> Path:
        key = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()
        return self.dir / f"{key}.json"

    def get(self, params: dict[str, str]) -> dict | None:
        path = self._path(params)
        try:
            if time.time() - path.stat().st_mtime > self.ttl:
                return None
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return None

    def set(self, params: dict[str, str], data: dict) -> None:
        path = self._path(params)
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data))
            os.replace(tmp, path)  # atomic: never leaves a half-written file
        except OSError as exc:
            log.warning("cache write failed: %s", exc)