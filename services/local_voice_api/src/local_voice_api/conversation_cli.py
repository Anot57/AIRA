"""CLI for one complete private local Aira conversation turn."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Sequence

from .approved_references import ApprovedReferenceValidationError
from .conversation import (
    DEFAULT_LLM_TIMEOUT_SECONDS,
    ConversationError,
    run_conversation_turn,
)
from .generation import VoiceGenerationError
from .synthesis import (
    DEFAULT_SYNTHESIS_SEED,
    VoiceSynthesisError,
    shutdown_voice_clone_runtime,
)
from .transcription import (
    DEFAULT_CPU_THREADS,
    TranscriptionError,
    shutdown_transcription_runtime,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run one private local audio conversation turn with an explicitly "
            "identified adult fictional AI companion."
        )
    )
    parser.add_argument(
        "--companion",
        required=True,
        help="approved local companion ID (Milestone 4C supports: aanya)",
    )
    parser.add_argument(
        "--audio",
        required=True,
        type=Path,
        help="input audio under /mnt/e/aira-local-runtime",
    )
    parser.add_argument(
        "--cpu-threads",
        type=int,
        default=DEFAULT_CPU_THREADS,
        help=f"Faster-Whisper CPU inference threads (default: {DEFAULT_CPU_THREADS})",
    )
    parser.add_argument(
        "--tts-seed",
        type=int,
        default=DEFAULT_SYNTHESIS_SEED,
        help=f"approved voice synthesis seed (default: {DEFAULT_SYNTHESIS_SEED})",
    )
    parser.add_argument(
        "--llm-timeout-seconds",
        type=float,
        default=DEFAULT_LLM_TIMEOUT_SECONDS,
        help=(
            "maximum llama.cpp generation time "
            f"(default: {DEFAULT_LLM_TIMEOUT_SECONDS:g} seconds)"
        ),
    )
    return parser


def _shutdown_local_runtimes() -> None:
    try:
        shutdown_transcription_runtime()
    finally:
        shutdown_voice_clone_runtime()


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    try:
        result = run_conversation_turn(
            companion_id=arguments.companion,
            audio_path=arguments.audio,
            cpu_threads=arguments.cpu_threads,
            tts_seed=arguments.tts_seed,
            llm_timeout_seconds=arguments.llm_timeout_seconds,
        )
    except (
        ApprovedReferenceValidationError,
        ConversationError,
        TranscriptionError,
        VoiceGenerationError,
        VoiceSynthesisError,
        ValueError,
    ) as error:
        parser.exit(status=1, message=f"error: {error}\n")
    finally:
        _shutdown_local_runtimes()

    print(f"AI companion: {arguments.companion}")
    print(f"Transcript: {result.normalized_transcript}")
    print(f"Response: {result.assistant_response}")
    print(f"WAV: {result.output_wav_path}")
    print(f"Turn metadata: {result.metadata_path}")
    return 0
