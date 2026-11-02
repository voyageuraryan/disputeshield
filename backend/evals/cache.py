"""Disk cache for eval runs (evals/.cache/, git-ignored).

Pipeline outputs are keyed by (golden id, prompt hash, model under test), so re-running the metrics never calls
the model under test again; judge answers are keyed by (judge model, prompt hash, schema). EVALS_REFRESH=1
(scripts/run_evals.sh --refresh) bypasses reads (results are still written).
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parent / ".cache"


def refresh() -> bool:
    return os.getenv("EVALS_REFRESH", "").strip().lower() in ("1", "true", "yes")


def digest(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode())
        h.update(b"\x00")
    return h.hexdigest()


class DiskCache:
    def __init__(self, name: str, root: Path = ROOT):
        self.dir = root / name
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in key)[:180]
        return self.dir / f"{safe}.json"

    def get(self, key: str) -> Any | None:
        if refresh():
            return None
        p = self._path(key)
        return json.loads(p.read_text()) if p.is_file() else None

    def set(self, key: str, value: Any) -> None:
        p = self._path(key)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(value, indent=1, default=str))
        tmp.replace(p)  # atomic: a crash never leaves a half-written entry
