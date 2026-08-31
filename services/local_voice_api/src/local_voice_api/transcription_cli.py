"""Command-line interface for private local speech-to-text."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .transcription import (
    DEFAULT_CPU_THREADS,
    DEFAULT_MODEL_DIR,
    DEFAULT_TRANSCRIPT_OUTPUT_DIR,
    TranscriptionError,
    describe_transcription_configuration,
    shutdown_transcription_runtime,
    transcribe_audio,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Transcribe one short audio turn locally with Faster-Whisper."
    )
    parser.add_argument(
        "--show-config",
        action="store_true",
        help="show local STT settings without importing Faster-Whisper",
    )
    parser.add_argument("--audio", type=Path, help="input audio file under /mnt/e")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_TRANSCRIPT_OUTPUT_DIR,
        help=f"E-drive transcript directory (default: {DEFAULT_TRANSCRIPT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--model-dir",
        type=Path,
        default=DEFAULT_MODEL_DIR,
        help=f"E-drive model download root (default: {DEFAULT_MODEL_DIR})",
    )
    parser.add_argument(
        "--cpu-threads",
        type=int,
        default=DEFAULT_CPU_THREADS,
        help=f"CPU inference threads (default: {DEFAULT_CPU_THREADS})",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)

    if arguments.show_config:
        try:
            configuration = describe_transcription_configuration(
                output_dir=arguments.output_dir,
                model_dir=arguments.model_dir,
                cpu_threads=arguments.cpu_threads,
            )
        except (TranscriptionError, ValueError) as error:
            parser.error(str(error))
        print(json.dumps(configuration, indent=2, sort_keys=True))
        return 0

    if arguments.audio is None:
        parser.error("--audio is required unless --show-config is used.")

    try:
        result = transcribe_audio(
            audio_path=arguments.audio,
            output_dir=arguments.output_dir,
            model_dir=arguments.model_dir,
            cpu_threads=arguments.cpu_threads,
        )
    except (TranscriptionError, ValueError) as error:
        parser.exit(status=1, message=f"error: {error}\n")
    finally:
        shutdown_transcription_runtime()

    print(f"Transcript: {result.transcript_path}")
    print(f"Metadata: {result.metadata_path}")
    return 0
