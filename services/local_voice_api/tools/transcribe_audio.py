#!/usr/bin/env python3
"""Run private local speech-to-text from a repository checkout."""

import sys
from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = SERVICE_ROOT / "src"
sys.path.insert(0, str(SOURCE_ROOT))

from local_voice_api.transcription_cli import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
