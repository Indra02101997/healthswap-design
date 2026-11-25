"""Locating the repository root (api/openapi.yaml, data/, db/schema.sql, models/)."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path


def find_repo_root(start: str | os.PathLike[str] | None = None) -> Path:
    """Walks up from `start` (default: this file, then the working directory) to the folder holding api/openapi.yaml.

    The HEALTHSWAP_REPO_ROOT environment variable wins when set.
    """
    env = os.environ.get("HEALTHSWAP_REPO_ROOT")
    if env:
        return Path(env).resolve()
    candidates = [Path(start).resolve()] if start is not None else [Path(__file__).resolve().parent, Path.cwd().resolve()]
    for origin in candidates:
        for folder in (origin, *origin.parents):
            if (folder / "api" / "openapi.yaml").is_file() and (folder / "data").is_dir():
                return folder
    raise FileNotFoundError("Could not locate the repository root (api/openapi.yaml); set HEALTHSWAP_REPO_ROOT")


@lru_cache(maxsize=1)
def repo_root() -> Path:
    return find_repo_root()
