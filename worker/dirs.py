"""User-data paths. Package code stays in the npm install; state lives in ~/buildplate."""

from __future__ import annotations

import os
from pathlib import Path


def home() -> Path:
    raw = os.environ.get("BUILDPLATE_HOME", "").strip()
    return Path(raw).expanduser() if raw else Path.home() / "buildplate"


def cache_dir() -> Path:
    raw = os.environ.get("BUILDPLATE_CACHE", "").strip()
    p = Path(raw).expanduser() if raw else home() / "cache"
    p.mkdir(parents=True, exist_ok=True)
    return p


def vendor_dir() -> Path:
    raw = os.environ.get("BUILDPLATE_VENDOR", "").strip()
    return Path(raw).expanduser() if raw else home() / "vendor"


def out_dir() -> Path:
    raw = os.environ.get("BUILDPLATE_OUT_DIR", "").strip()
    return Path(raw).expanduser() if raw else home() / "out"
