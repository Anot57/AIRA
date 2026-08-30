"""Command-line entry point for Local Voice Lab reference generation."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from .generation import (
    DEFAULT_OUTPUT_DIR,
    DEFAULT_SEED,
    VoiceGenerationError,
    generate_voice_reference,
)
from .profiles import VOICE_PROFILES, get_voice_profile


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="List adult fictional AI voices or generate one local reference."
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="list validated companion voice profiles without loading Qwen",
    )
    parser.add_argument(
        "--companion",
        default="aanya",
        help="companion ID to generate (default: aanya)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"E-drive output directory (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=DEFAULT_SEED,
        help=f"requested generation seed (default: {DEFAULT_SEED})",
    )
    return parser


def _list_profiles() -> None:
    for profile in VOICE_PROFILES:
        print(f"{profile.companion_id}: {profile.voice_description}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)

    if arguments.list:
        _list_profiles()
        return 0

    try:
        profile = get_voice_profile(arguments.companion)
    except ValueError as error:
        parser.error(str(error))

    try:
        result = generate_voice_reference(
            profile=profile,
            output_dir=arguments.output_dir,
            seed=arguments.seed,
        )
    except VoiceGenerationError as error:
        parser.exit(status=1, message=f"error: {error}\n")

    print(f"WAV: {result.wav_path}")
    print(f"Metadata: {result.metadata_path}")
    return 0

