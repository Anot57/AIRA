"""Development-server CLI for the trusted-local-network Aira voice API."""

from __future__ import annotations

import argparse
import logging
from typing import Any, Sequence

DEFAULT_API_HOST = "0.0.0.0"
DEFAULT_API_PORT = 8765


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the unauthenticated Aira voice bridge for trusted local-network "
            "development only. Never expose it to the public internet."
        )
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_API_HOST,
        help=f"development bind host (default: {DEFAULT_API_HOST})",
    )
    parser.add_argument(
        "--port",
        default=DEFAULT_API_PORT,
        type=int,
        help=f"development bind port (default: {DEFAULT_API_PORT})",
    )
    return parser


def _load_uvicorn() -> Any:
    try:
        import uvicorn
    except (ImportError, ModuleNotFoundError) as error:
        raise RuntimeError(
            "Uvicorn is not installed. Install "
            "services/local_voice_api/requirements-api.txt in the E-drive runtime."
        ) from error
    return uvicorn


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    if not isinstance(arguments.host, str) or not arguments.host.strip():
        parser.error("--host must be a non-empty hostname or address.")
    if not 1 <= arguments.port <= 65_535:
        parser.error("--port must be between 1 and 65535.")

    try:
        uvicorn = _load_uvicorn()
    except RuntimeError as error:
        parser.exit(status=1, message=f"error: {error}\n")

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    logging.warning(
        "Starting an unauthenticated local-development API on %s:%s. "
        "Use only on a trusted local network; never port-forward it publicly.",
        arguments.host,
        arguments.port,
    )
    uvicorn.run(
        "local_voice_api.api:app",
        host=arguments.host,
        port=arguments.port,
        workers=1,
        reload=False,
    )
    return 0
