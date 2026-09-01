#!/usr/bin/env python3
"""Run the trusted-local-network Aira voice API from a repository checkout."""

import sys

# Keep this developer runtime from creating bytecode caches in the repository.
sys.dont_write_bytecode = True

from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = SERVICE_ROOT / "src"
sys.path.insert(0, str(SOURCE_ROOT))

from local_voice_api.api_cli import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
