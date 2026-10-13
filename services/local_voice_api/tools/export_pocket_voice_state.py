#!/usr/bin/env python3
"""Encode approved companion voices into Pocket TTS voice states.

Run with the dedicated Pocket TTS Windows Python. Only voices listed in
config/approved_voice_references.json are exported, after the same metadata
and WAV checks synthesis uses; the worker then loads each
"<companion>_voice.safetensors" beside Aanya's on its next start.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

SERVICE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_ROOT / "src"))

from local_voice_api.approved_references import (  # noqa: E402
    APPROVED_VOICE_REFERENCES_BY_ID,
    load_validated_voice_reference,
)
from local_voice_api.pocket_tts_worker import (  # noqa: E402
    DEFAULT_CACHE_ROOT,
    DEFAULT_RUNTIME_ROOT,
    VOICE_STATE_SUFFIX,
)

DEFAULT_REFERENCE_DIR = Path(r"E:\aira-local-runtime\generated\voices")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--companion", help="One approved companion ID.")
    selection.add_argument(
        "--all-approved",
        action="store_true",
        help="Every approved companion without an exported state yet.",
    )
    parser.add_argument("--reference-dir", type=Path, default=DEFAULT_REFERENCE_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_RUNTIME_ROOT)
    parser.add_argument("--cache-root", type=Path, default=DEFAULT_CACHE_ROOT)
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace an existing state, e.g. after approving a new seed.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output_dir = args.output_dir.resolve(strict=True)
    if args.companion:
        companions = [args.companion]
    else:
        companions = [
            companion_id
            for companion_id in APPROVED_VOICE_REFERENCES_BY_ID
            if not (output_dir / f"{companion_id}{VOICE_STATE_SUFFIX}").exists()
        ]
    # Validate every reference before loading the model.
    references = {
        companion_id: load_validated_voice_reference(companion_id, args.reference_dir)
        for companion_id in companions
    }
    for companion_id in companions:
        destination = output_dir / f"{companion_id}{VOICE_STATE_SUFFIX}"
        if destination.exists() and not args.overwrite:
            raise SystemExit(
                f"{destination} already exists; pass --overwrite to replace it."
            )
    if not references:
        print("Every approved voice already has an exported state.")
        return 0

    cache_root = args.cache_root.resolve(strict=True)
    os.environ["HF_HOME"] = str(cache_root / "huggingface")
    os.environ["HF_HUB_CACHE"] = str(cache_root / "huggingface" / "hub")
    os.environ["XDG_CACHE_HOME"] = str(cache_root)

    from pocket_tts import TTSModel, export_model_state

    model = TTSModel.load_model()
    for companion_id, reference in references.items():
        destination = output_dir / f"{companion_id}{VOICE_STATE_SUFFIX}"
        state = model.get_state_for_audio_prompt(str(reference.wav_path))
        export_model_state(state, str(destination))
        print(f"{companion_id}: {reference.wav_path.name} -> {destination}")
    print("Restart the Pocket TTS worker to load the new voices.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
