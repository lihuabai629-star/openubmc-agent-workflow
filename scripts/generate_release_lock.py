#!/usr/bin/env python3
"""Generate or verify the repository's immutable release-lock.json."""

from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "openubmc-target-runtime"))

from openubmc_target_runtime.release import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
