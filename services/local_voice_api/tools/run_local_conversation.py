#!/usr/bin/env python3
"""Run one complete local Aira conversation turn from a repository checkout."""

import sys

# The runtime environment is configured after this launcher imports the package.
# Prevent those early imports from creating repository-local bytecode caches.
sys.dont_write_bytecode = True

from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = SERVICE_ROOT / "src"
sys.path.insert(0, str(SOURCE_ROOT))

from local_voice_api.conversation_cli import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
