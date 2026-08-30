#!/usr/bin/env python3
"""Run Local Voice Lab from a repository checkout without installing it."""

from pathlib import Path
import sys

SERVICE_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = SERVICE_ROOT / "src"
sys.path.insert(0, str(SOURCE_ROOT))

from local_voice_api.cli import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())

