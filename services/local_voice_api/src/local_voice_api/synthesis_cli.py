"""CLI for synthesis with explicitly approved companion voice references."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Sequence

from .approved_references import (
    APPROVED_VOICE_REFERENCES,
    ApprovedReferenceValidationError,
    get_approved_voice_reference,
)
from .generation import VoiceGenerationError
from .synthesis import (
    DEFAULT_REFERENCE_DIR,
    DEFAULT_SYNTHESIS_OUTPUT_DIR,
    DEFAULT_SYNTHESIS_SEED,
    MAX_SYNTHESIS_TEXT_CHARACTERS,
    VoiceSynthesisError,
    shutdown_voice_clone_runtime,
    synthesize_companion_voice,
    validate_synthesis_text,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Synthesize local speech from an approved adult AI voice."
    )
    parser.add_argument(
        "--list-approved",
        action="store_true",
        help="list approved references without importing the model runtime",
    )
    parser.add_argument(
        "--companion",
        default="aanya",
        help="approved companion ID (default: aanya)",
    )
    parser.add_argument(
        "--text",
        help=(
            "text to synthesize; required unless --list-approved is used "
            f"(maximum {MAX_SYNTHESIS_TEXT_CHARACTERS} characters)"
        ),
    )
    parser.add_argument(
        "--reference-dir",
        type=Path,
        default=DEFAULT_REFERENCE_DIR,
        help=f"approved E-drive reference directory (default: {DEFAULT_REFERENCE_DIR})",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_SYNTHESIS_OUTPUT_DIR,
        help=(
            "E-drive synthesis output directory "
            f"(default: {DEFAULT_SYNTHESIS_OUTPUT_DIR})"
        ),
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=DEFAULT_SYNTHESIS_SEED,
        help=f"output generation seed (default: {DEFAULT_SYNTHESIS_SEED})",
    )
    return parser


def _list_approved_references() -> None:
    for reference in APPROVED_VOICE_REFERENCES:
        print(
            f"{reference.companion_id}: seed={reference.seed} "
            f"wav={reference.wav_filename} "
            f"metadata={reference.metadata_filename}"
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)

    if arguments.list_approved:
        _list_approved_references()
        return 0

    if arguments.text is None:
        parser.error("--text is required for synthesis.")

    try:
        get_approved_voice_reference(arguments.companion)
        validate_synthesis_text(arguments.text)
    except (ApprovedReferenceValidationError, ValueError) as error:
        parser.error(str(error))

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        result = synthesize_companion_voice(
            companion_id=arguments.companion,
            text=arguments.text,
            reference_dir=arguments.reference_dir,
            output_dir=arguments.output_dir,
            seed=arguments.seed,
        )
    except (
        ApprovedReferenceValidationError,
        VoiceGenerationError,
        VoiceSynthesisError,
        ValueError,
    ) as error:
        parser.exit(status=1, message=f"error: {error}\n")
    finally:
        shutdown_voice_clone_runtime()

    print(f"WAV: {result.wav_path}")
    print(f"Metadata: {result.metadata_path}")
    return 0
