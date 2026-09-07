"""Reusable local Base-model runtime for approved companion voices."""

from __future__ import annotations

import gc
import hashlib
import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .approved_references import (
    ApprovedReferenceValidationError,
    ValidatedVoiceReference,
    load_validated_voice_reference,
)
from .generation import (
    _is_cuda_out_of_memory,
    _seed_runtime,
    validate_e_drive_runtime,
)
from .observability import current_turn_timing

BASE_MODEL_ID = "Qwen/Qwen3-TTS-12Hz-0.6B-Base"
DEFAULT_REFERENCE_DIR = Path("/mnt/e/aira-local-runtime/generated/voices")
DEFAULT_SYNTHESIS_OUTPUT_DIR = Path(
    "/mnt/e/aira-local-runtime/generated/runtime"
)
DEFAULT_SYNTHESIS_SEED = 20260901
MAX_SYNTHESIS_TEXT_CHARACTERS = 500

_LOGGER = logging.getLogger(__name__)
_CUDA_ASSERTION_SIGNATURES = (
    "device-side assert",
    "probability tensor contains either inf, nan or element < 0",
)
_POISONED_CUDA_MESSAGE = (
    "A CUDA device-side assertion occurred during Qwen Base synthesis. "
    "The CUDA process is poisoned and cannot safely retry in-process. Change "
    "the Base-model precision/runtime settings, then start a fresh process "
    "before synthesizing again."
)


class VoiceSynthesisError(RuntimeError):
    """Raised for an actionable local companion synthesis failure."""


@dataclass(frozen=True, slots=True)
class SynthesisResult:
    """Files written by one approved companion synthesis."""

    wav_path: Path
    metadata_path: Path
    sample_rate: int


@dataclass(frozen=True, slots=True)
class BaseRuntimeConfiguration:
    """Resolved CUDA settings for the process-wide Qwen Base model."""

    gpu_name: str
    compute_capability: tuple[int, int]
    dtype: Any
    dtype_name: str
    attention_backend: str

    @property
    def compute_capability_label(self) -> str:
        return f"{self.compute_capability[0]}.{self.compute_capability[1]}"


@dataclass(frozen=True, slots=True)
class _CachedVoiceClonePrompt:
    signature: tuple[object, ...]
    prompt: Any


_runtime_lock = threading.RLock()
_torch_module: Any = None
_numpy_module: Any = None
_soundfile_module: Any = None
_base_model: Any = None
_base_runtime: BaseRuntimeConfiguration | None = None
_prompt_cache: dict[str, _CachedVoiceClonePrompt] = {}
_runtime_shutdown = False
_runtime_poisoned = False


def validate_synthesis_text(text: object) -> str:
    """Require nonempty text no longer than the local synthesis limit."""

    if not isinstance(text, str) or not text.strip():
        raise ValueError("Synthesis text must be a non-empty string.")
    if len(text) > MAX_SYNTHESIS_TEXT_CHARACTERS:
        raise ValueError(
            "Synthesis text must not exceed "
            f"{MAX_SYNTHESIS_TEXT_CHARACTERS} characters."
        )
    return text


def _validate_output_seed(seed: object) -> int:
    if type(seed) is not int or seed < 0:
        raise ValueError("Output seed must be a non-negative integer.")
    return seed


def select_base_runtime(
    torch_module: Any, device_index: int = 0
) -> BaseRuntimeConfiguration:
    """Select stable Base-model settings from an injected CUDA device."""

    gpu_name = str(torch_module.cuda.get_device_name(device_index))
    raw_capability = torch_module.cuda.get_device_capability(device_index)
    try:
        major, minor = raw_capability
    except (TypeError, ValueError) as error:
        raise VoiceSynthesisError(
            f"CUDA returned an invalid compute capability: {raw_capability!r}."
        ) from error
    if (
        type(major) is not int
        or type(minor) is not int
        or major < 0
        or minor < 0
    ):
        raise VoiceSynthesisError(
            f"CUDA returned an invalid compute capability: {raw_capability!r}."
        )

    if major < 8:
        dtype = torch_module.float32
        dtype_name = "float32"
        attention_backend = "eager"
    else:
        dtype = torch_module.float16
        dtype_name = "float16"
        attention_backend = "sdpa"

    return BaseRuntimeConfiguration(
        gpu_name=gpu_name,
        compute_capability=(major, minor),
        dtype=dtype,
        dtype_name=dtype_name,
        attention_backend=attention_backend,
    )


def _is_cuda_assertion(error: Exception) -> bool:
    """Recognize a device assertion and its confirmed probability precursor."""

    message = str(error).casefold()
    return any(signature in message for signature in _CUDA_ASSERTION_SIGNATURES)


def _load_torch() -> Any:
    try:
        import torch
    except (ImportError, ModuleNotFoundError) as error:
        raise VoiceSynthesisError(
            "PyTorch is not installed. Activate the documented E-drive WSL "
            "environment and install the CUDA runtime before synthesis."
        ) from error
    return torch


def _load_model_dependencies() -> tuple[Any, Any, Any]:
    try:
        import numpy
        import soundfile
        from qwen_tts import Qwen3TTSModel
    except (ImportError, ModuleNotFoundError) as error:
        dependency = getattr(error, "name", None) or "Qwen voice runtime"
        raise VoiceSynthesisError(
            f"Missing runtime dependency {dependency!r}. Activate the "
            "documented E-drive WSL environment and install qwen-tts."
        ) from error
    return numpy, soundfile, Qwen3TTSModel


def _ensure_base_model() -> Any:
    """Lazily load the single process-wide Base model."""

    global _base_model
    global _base_runtime
    global _numpy_module
    global _runtime_poisoned
    global _soundfile_module
    global _torch_module

    if _runtime_poisoned:
        raise VoiceSynthesisError(_POISONED_CUDA_MESSAGE)
    if _runtime_shutdown:
        raise VoiceSynthesisError(
            "The voice synthesis runtime has been shut down. Start a new "
            "process before synthesizing again."
        )
    timing = current_turn_timing()
    if _base_model is not None:
        if _base_runtime is None:
            raise VoiceSynthesisError(
                "The cached Qwen Base model has no resolved runtime settings. "
                "Start a fresh process before synthesizing again."
            )
        if timing is not None:
            timing.record(
                "qwen_base_model_load", 0.0, cold=False, cache_hit=True
            )
        return _base_model

    torch = _load_torch()
    if not torch.cuda.is_available():
        raise VoiceSynthesisError(
            "CUDA is required for companion voice synthesis, but "
            "torch.cuda.is_available() returned False. Verify the NVIDIA "
            "driver and WSL CUDA access with nvidia-smi."
        )

    runtime = select_base_runtime(torch)
    _LOGGER.info(
        "Selected Qwen Base runtime: GPU=%s; compute capability=%s; "
        "dtype=torch.%s; attention backend=%s",
        runtime.gpu_name,
        runtime.compute_capability_label,
        runtime.dtype_name,
        runtime.attention_backend,
    )
    numpy, soundfile, qwen_model_class = _load_model_dependencies()
    try:
        if timing is None:
            model = qwen_model_class.from_pretrained(
                BASE_MODEL_ID,
                device_map="cuda:0",
                dtype=runtime.dtype,
                attn_implementation=runtime.attention_backend,
            )
        else:
            with timing.stage(
                "qwen_base_model_load", cold=True, cache_hit=False
            ):
                model = qwen_model_class.from_pretrained(
                    BASE_MODEL_ID,
                    device_map="cuda:0",
                    dtype=runtime.dtype,
                    attn_implementation=runtime.attention_backend,
                )
    except Exception as error:
        if _is_cuda_assertion(error):
            _runtime_poisoned = True
            raise VoiceSynthesisError(_POISONED_CUDA_MESSAGE) from error
        gc.collect()
        try:
            torch.cuda.empty_cache()
        except Exception:
            pass
        if _is_cuda_out_of_memory(torch, error):
            raise VoiceSynthesisError(
                "CUDA ran out of memory while loading the Qwen Base model. "
                "Close other GPU workloads and retry; CPU fallback is disabled."
            ) from error
        raise VoiceSynthesisError(
            f"Could not load the Qwen Base model: {error}"
        ) from error

    _torch_module = torch
    _numpy_module = numpy
    _soundfile_module = soundfile
    _base_model = model
    _base_runtime = runtime
    return _base_model


def _get_voice_clone_prompt(
    model: Any, reference: ValidatedVoiceReference
) -> Any:
    signature = reference.cache_signature()
    companion_id = reference.approved.companion_id
    cached_prompt = _prompt_cache.get(companion_id)
    timing = current_turn_timing()
    cache_hit = cached_prompt is not None and cached_prompt.signature == signature
    if timing is not None:
        timing.record("voice_prompt_cache_lookup", 0.0, cache_hit=cache_hit)
    if cache_hit:
        return cached_prompt.prompt

    if timing is None:
        prompt = model.create_voice_clone_prompt(
            ref_audio=str(reference.wav_path),
            ref_text=reference.transcript,
            x_vector_only_mode=False,
        )
    else:
        with timing.stage("voice_prompt_creation", cache_hit=False):
            prompt = model.create_voice_clone_prompt(
                ref_audio=str(reference.wav_path),
                ref_text=reference.transcript,
                x_vector_only_mode=False,
            )
    _prompt_cache[companion_id] = _CachedVoiceClonePrompt(
        signature=signature,
        prompt=prompt,
    )
    return prompt


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _output_paths(
    companion_id: str, text: str, seed: int, output_dir: Path
) -> tuple[Path, Path]:
    text_digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    stem = f"{companion_id}_runtime_seed_{seed}_{text_digest}"
    wav_path = output_dir / f"{stem}.wav"
    metadata_path = output_dir / f"{stem}.json"
    for output_path in (wav_path, metadata_path):
        if output_path.is_symlink():
            raise VoiceSynthesisError(
                f"Synthesis output path must not be a symlink: {output_path}."
            )
    return wav_path, metadata_path


def synthesize_companion_voice(
    companion_id: str,
    text: str,
    reference_dir: os.PathLike[str] | str = DEFAULT_REFERENCE_DIR,
    output_dir: os.PathLike[str] | str = DEFAULT_SYNTHESIS_OUTPUT_DIR,
    seed: int = DEFAULT_SYNTHESIS_SEED,
) -> SynthesisResult:
    """Synthesize text with one exact approved reference and cached prompt."""

    global _runtime_poisoned

    validated_text = validate_synthesis_text(text)
    output_seed = _validate_output_seed(seed)
    validate_e_drive_runtime(reference_dir)
    validate_e_drive_runtime(output_dir)
    reference = load_validated_voice_reference(companion_id, reference_dir)

    destination = Path(output_dir).expanduser().resolve(strict=False)
    destination.mkdir(parents=True, exist_ok=True)
    wav_path, metadata_path = _output_paths(
        companion_id, validated_text, output_seed, destination
    )

    waveforms = None
    with _runtime_lock:
        try:
            model = _ensure_base_model()
            runtime = _base_runtime
            if runtime is None:
                raise VoiceSynthesisError(
                    "Qwen Base runtime settings were not resolved. Start a "
                    "fresh process before synthesizing again."
                )
            prompt = _get_voice_clone_prompt(model, reference)
            _seed_runtime(output_seed, _numpy_module, _torch_module)
            timing = current_turn_timing()
            if timing is None:
                waveforms, sample_rate = model.generate_voice_clone(
                    text=validated_text,
                    language="English",
                    voice_clone_prompt=prompt,
                )
            else:
                with timing.stage(
                    "tts_generation", response_characters=len(validated_text)
                ):
                    waveforms, sample_rate = model.generate_voice_clone(
                        text=validated_text,
                        language="English",
                        voice_clone_prompt=prompt,
                    )
            if not waveforms:
                raise VoiceSynthesisError("Qwen returned no synthesized waveform.")

            sample_rate = int(sample_rate)
            if sample_rate <= 0:
                raise VoiceSynthesisError(
                    "Qwen returned an invalid synthesis sample rate."
                )
            if timing is None:
                _soundfile_module.write(
                    str(wav_path),
                    waveforms[0],
                    sample_rate,
                    format="WAV",
                    subtype="PCM_16",
                )
            else:
                try:
                    sample_count = len(waveforms[0])
                except TypeError:
                    # Keep timing instrumentation compatible with scalar-like
                    # test/runtime containers accepted by SoundFile.
                    sample_count = 1
                audio_duration = sample_count / sample_rate
                with timing.stage(
                    "wav_serialization_write",
                    audio_duration_seconds=round(audio_duration, 3),
                    generated_sample_count=sample_count,
                ):
                    _soundfile_module.write(
                        str(wav_path),
                        waveforms[0],
                        sample_rate,
                        format="WAV",
                        subtype="PCM_16",
                    )
            metadata = {
                "adult": True,
                "ai_generated": True,
                "attention_backend": runtime.attention_backend,
                "companion_id": companion_id,
                "cuda_compute_capability": runtime.compute_capability_label,
                "created_at_utc": _utc_timestamp(),
                "model_id": BASE_MODEL_ID,
                "output_seed": output_seed,
                "reference_seed": reference.approved.seed,
                "reference_wav_filename": reference.approved.wav_filename,
                "runtime_dtype": runtime.dtype_name,
                "sample_rate": sample_rate,
                "synthesized_text": validated_text,
            }
            with metadata_path.open(
                "w", encoding="utf-8", newline="\n"
            ) as metadata_file:
                json.dump(
                    metadata,
                    metadata_file,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                metadata_file.write("\n")

            return SynthesisResult(
                wav_path=wav_path,
                metadata_path=metadata_path,
                sample_rate=sample_rate,
            )
        except (ApprovedReferenceValidationError, VoiceSynthesisError):
            raise
        except Exception as error:
            if _is_cuda_assertion(error):
                _runtime_poisoned = True
                raise VoiceSynthesisError(_POISONED_CUDA_MESSAGE) from error
            if _is_cuda_out_of_memory(_torch_module, error):
                raise VoiceSynthesisError(
                    "CUDA ran out of memory during companion voice synthesis. "
                    "Close other GPU workloads and retry; CPU fallback is disabled."
                ) from error
            raise VoiceSynthesisError(
                f"Companion voice synthesis failed: {error}"
            ) from error
        finally:
            waveforms = None


def warmup_qwen_base_model() -> None:
    """Load the process-wide CUDA Base model without generating speech."""

    with _runtime_lock:
        _ensure_base_model()


def warmup_voice_clone_prompt(
    companion_id: str = "aanya",
    reference_dir: os.PathLike[str] | str = DEFAULT_REFERENCE_DIR,
) -> None:
    """Create and cache the approved voice prompt without synthesizing speech."""

    validate_e_drive_runtime(reference_dir)
    reference = load_validated_voice_reference(companion_id, reference_dir)
    with _runtime_lock:
        model = _ensure_base_model()
        _get_voice_clone_prompt(model, reference)


def shutdown_voice_clone_runtime() -> None:
    """Permanently release cached prompts, the Base model, and CUDA memory."""

    global _base_model
    global _base_runtime
    global _numpy_module
    global _runtime_shutdown
    global _soundfile_module
    global _torch_module

    with _runtime_lock:
        torch = _torch_module
        _prompt_cache.clear()
        _base_model = None
        _base_runtime = None
        _numpy_module = None
        _soundfile_module = None
        _torch_module = None
        _runtime_shutdown = True
        if not _runtime_poisoned:
            gc.collect()
        if torch is not None and not _runtime_poisoned:
            try:
                torch.cuda.empty_cache()
            except Exception:
                # Shutdown cleanup is best-effort and must remain safe to call.
                pass
