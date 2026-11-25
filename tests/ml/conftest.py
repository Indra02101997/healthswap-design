"""The ml/ tools are plain scripts (python ml/train.py, python ml/finetune/build_datasets.py, python ml/finetune/submit.py); put their
folders on sys.path, as running them does, so the tests can import them as modules (train, build_datasets, submit)."""

from __future__ import annotations

import sys

from healthswap.paths import repo_root

for _folder in (repo_root() / "ml", repo_root() / "ml" / "finetune"):
    if str(_folder) not in sys.path:
        sys.path.insert(0, str(_folder))
