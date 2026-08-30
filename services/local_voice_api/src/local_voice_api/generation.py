"""Lazy Qwen VoiceDesign reference generation for CUDA workstations."""

from __future__ import annotations

import gc
import json
import os
import posixpath
import random
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .profiles import VoiceProfile

MODEL_ID = "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign"
DEFAULT_OUTPUT_DIR = Path("/mnt/e/aira-local-runtime/generated/voices")
DEFAULT_SEED = 20260830

REQUIRED_E_DRIVE_ENVIRONMENT = (
    "VIRTUAL_ENV",
    "HF_HOME",
    "TORCH_HOME",
    "XDG_CACHE_HOME",
    "PIP_CACHE_DIR",
    "TMPDIR",
    "CUDA_CACHE_PATH",
    "NUMBA_CACHE_DIR",
    "TORCHINDUCTOR_CACHE_DIR",
    "TRITON_CACHE_DIR",
    "PYTHONPYCACHEPREFIX",
)
OPTIONAL_CACHE_OVERRIDES = (
    "HF_HUB_CACHE",
    "HUGGINGFACE_HUB_CACHE",
    "TRANSFORMERS_CACHE",
    "HF_XET_CACHE",
    "HF_ASSETS_CACHE",
    "HF_MODULES_CACHE",
    "HF_TOKEN_PATH",
    "TORCH_EXTENSIONS_DIR",
)


class VoiceGenerationError(RuntimeError):
    """Raised for an actionable local voice generation failure."""


@dataclass(frozen=True, slots=True)
class GenerationResult:
    """Paths written by one reference generation."""

    wav_path: Path
    metadata_path: Path
    sample_rate: int


def is_e_drive_path(path: os.PathLike[str] | str) -> bool:
    """Return whether a WSL or Windows path is rooted on the E drive."""

    raw_path = os.fspath(path).replace("\\", "/")
    if re.match(r"^[A-Za-z]:", raw_path):
        return len(raw_path) >= 3 and raw_path[:3].casefold() == "e:/"

    normalised = posixpath.normpath(raw_path)
    return normalised == "/mnt/e" or normalised.startswith("/mnt/e/")


def _resolves_to_e_drive(path: os.PathLike[str] | str) -> bool:
    """Resolve existing symlinks so an E-drive-looking path cannot escape."""

    try:
        resolved_path = Path(path).expanduser().resolve(strict=False)
    except (OSError, RuntimeError):
        return False
    return is_e_drive_path(resolved_path)


def validate_e_drive_runtime(
    output_dir: os.PathLike[str] | str,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Prevent generated files, model data, and runtime caches leaving E drive."""

    environment = os.environ if environ is None else environ
    problems: list[str] = []

    if not is_e_drive_path(output_dir) or not _resolves_to_e_drive(output_dir):
        problems.append(f"output directory {os.fspath(output_dir)!r}")

    for variable in REQUIRED_E_DRIVE_ENVIRONMENT:
        value = environment.get(variable)
        if not value:
            problems.append(f"unset {variable}")
        elif not is_e_drive_path(value) or not _resolves_to_e_drive(value):
            problems.append(f"{variable}={value!r}")

    for variable in OPTIONAL_CACHE_OVERRIDES:
        value = environment.get(variable)
        if value and (
            not is_e_drive_path(value) or not _resolves_to_e_drive(value)
        ):
            problems.append(f"{variable}={value!r}")

    if problems:
        details = "; ".join(problems)
        raise VoiceGenerationError(
            "Generation is restricted to E-drive runtime storage. Source "
            "/mnt/e/aira-local-runtime/activate.sh and use an output path under "
            f"/mnt/e before retrying. Invalid settings: {details}."
        )


def select_attention_implementation(torch_module: Any) -> str:
    """Prefer standard PyTorch SDPA and fall back to eager when unavailable."""

    torch_nn = getattr(torch_module, "nn", None)
    functional = getattr(torch_nn, "functional", None)
    sdpa = getattr(functional, "scaled_dot_product_attention", None)
    return "sdpa" if callable(sdpa) else "eager"


def _load_torch() -> Any:
    try:
        import torch
    except (ImportError, ModuleNotFoundError) as error:
        raise VoiceGenerationError(
            "PyTorch is not installed. Activate the documented E-drive WSL "
            "environment and install the CUDA build before generating."
        ) from error
    return torch


def _load_model_dependencies() -> tuple[Any, Any, Any]:
    try:
        import numpy
        import soundfile
        from qwen_tts import Qwen3TTSModel
    except (ImportError, ModuleNotFoundError) as error:
        dependency = getattr(error, "name", None) or "Qwen voice runtime"
        raise VoiceGenerationError(
            f"Missing runtime dependency {dependency!r}. Activate the documented "
            "E-drive WSL environment and install qwen-tts before generating."
        ) from error
    return numpy, soundfile, Qwen3TTSModel


def _is_cuda_out_of_memory(torch_module: Any, error: Exception) -> bool:
    oom_type = getattr(getattr(torch_module, "cuda", None), "OutOfMemoryError", None)
    if isinstance(oom_type, type) and isinstance(error, oom_type):
        return True
    return isinstance(error, RuntimeError) and "out of memory" in str(error).casefold()


def _seed_runtime(seed: int, numpy_module: Any, torch_module: Any) -> None:
    runtime_seed = seed % (2**32)
    random.seed(runtime_seed)
    numpy_module.random.seed(runtime_seed)
    torch_module.manual_seed(runtime_seed)
    torch_module.cuda.manual_seed_all(runtime_seed)


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def generate_voice_reference(
    profile: VoiceProfile,
    output_dir: os.PathLike[str] | str = DEFAULT_OUTPUT_DIR,
    seed: int = DEFAULT_SEED,
) -> GenerationResult:
    """Generate one WAV reference and its JSON metadata with one Qwen model."""

    validate_e_drive_runtime(output_dir)
    torch = _load_torch()
    if not torch.cuda.is_available():
        raise VoiceGenerationError(
            "CUDA is required for Local Voice Lab, but torch.cuda.is_available() "
            "returned False. Verify the NVIDIA driver and WSL CUDA access with "
            "nvidia-smi; CPU generation is not supported."
        )

    numpy, soundfile, qwen_model_class = _load_model_dependencies()
    attention_implementation = select_attention_implementation(torch)
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    stem = f"{profile.companion_id}_seed_{seed}"
    wav_path = destination / f"{stem}.wav"
    metadata_path = destination / f"{stem}.json"

    model = None
    waveforms = None
    try:
        _seed_runtime(seed, numpy, torch)
        model = qwen_model_class.from_pretrained(
            MODEL_ID,
            device_map="cuda:0",
            dtype=torch.float16,
            attn_implementation=attention_implementation,
        )
        _seed_runtime(seed, numpy, torch)
        waveforms, sample_rate = model.generate_voice_design(
            text=profile.reference_transcript,
            language=profile.language,
            instruct=profile.voice_description,
        )
        if not waveforms:
            raise VoiceGenerationError("Qwen returned no audio waveform.")

        sample_rate = int(sample_rate)
        soundfile.write(
            str(wav_path),
            waveforms[0],
            sample_rate,
            format="WAV",
            subtype="PCM_16",
        )
        metadata = {
            "adult": True,
            "ai_generated": True,
            "companion_id": profile.companion_id,
            "created_at_utc": _utc_timestamp(),
            "model_id": MODEL_ID,
            "sample_rate": sample_rate,
            "seed": seed,
            "transcript": profile.reference_transcript,
            "voice_description": profile.voice_description,
        }
        with metadata_path.open("w", encoding="utf-8", newline="\n") as metadata_file:
            json.dump(
                metadata,
                metadata_file,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            metadata_file.write("\n")

        return GenerationResult(
            wav_path=wav_path,
            metadata_path=metadata_path,
            sample_rate=sample_rate,
        )
    except VoiceGenerationError:
        raise
    except Exception as error:
        if _is_cuda_out_of_memory(torch, error):
            raise VoiceGenerationError(
                "CUDA ran out of memory while loading or running Qwen VoiceDesign. "
                "Close other GPU workloads and retry; CPU fallback is disabled."
            ) from error
        raise VoiceGenerationError(
            f"Voice reference generation failed: {error}"
        ) from error
    finally:
        waveforms = None
        model = None
        gc.collect()
        try:
            torch.cuda.empty_cache()
        except Exception:
            # Cache cleanup must not hide the original generation result or error.
            pass
