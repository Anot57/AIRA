"""Private, reusable Faster-Whisper runtime for short local audio turns."""

from __future__ import annotations

import gc
import hashlib
import json
import math
import os
import posixpath
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MODEL_NAME = "base.en"
DEVICE = "cpu"
COMPUTE_TYPE = "int8"
DEFAULT_CPU_THREADS = 6
NUM_WORKERS = 1
LANGUAGE = "en"
BEAM_SIZE = 1
VAD_FILTER = True
CONDITION_ON_PREVIOUS_TEXT = False
DEFAULT_MODEL_DIR = Path("/mnt/e/aira-local-runtime/models/faster-whisper")
DEFAULT_TRANSCRIPT_OUTPUT_DIR = Path(
    "/mnt/e/aira-local-runtime/generated/transcripts"
)
SUPPORTED_AUDIO_EXTENSIONS = frozenset({".wav", ".mp3", ".m4a", ".ogg", ".webm"})
MAX_INPUT_BYTES = 50 * 1024 * 1024


class TranscriptionError(RuntimeError):
    """Raised for an actionable local transcription failure."""


@dataclass(frozen=True, slots=True)
class TranscriptionRuntimeConfiguration:
    """Resolved settings for one process-wide Faster-Whisper model."""

    model_name: str
    device: str
    compute_type: str
    cpu_threads: int
    num_workers: int
    model_dir: Path


@dataclass(frozen=True, slots=True)
class TranscriptSegment:
    """One normalized transcript segment with timestamps in seconds."""

    start: float
    end: float
    text: str


@dataclass(frozen=True, slots=True)
class TranscriptionResult:
    """Local files and decoded content produced by one transcription."""

    transcript_path: Path
    metadata_path: Path
    transcript: str
    detected_language: str
    language_probability: float
    duration: float
    segments: tuple[TranscriptSegment, ...]


_runtime_lock = threading.RLock()
_whisper_model: Any = None
_loaded_runtime_config: TranscriptionRuntimeConfiguration | None = None
_runtime_shutdown = False


def is_e_drive_path(path: os.PathLike[str] | str) -> bool:
    """Return whether a WSL or Windows path is rooted on the E drive."""

    raw_path = os.fspath(path).replace("\\", "/")
    if re.match(r"^[A-Za-z]:", raw_path):
        return len(raw_path) >= 3 and raw_path[:3].casefold() == "e:/"

    normalised = posixpath.normpath(raw_path)
    return normalised == "/mnt/e" or normalised.startswith("/mnt/e/")


def validate_cpu_threads(cpu_threads: object) -> int:
    """Require a positive, non-boolean CPU thread count."""

    if type(cpu_threads) is not int or cpu_threads <= 0:
        raise ValueError("CPU thread count must be a positive integer.")
    return cpu_threads


def validate_audio_input(audio_path: os.PathLike[str] | str) -> Path:
    """Resolve and validate one local input before any model import occurs."""

    candidate = Path(audio_path).expanduser()
    if not is_e_drive_path(candidate):
        raise TranscriptionError(
            f"Input audio must be stored under /mnt/e: {candidate}."
        )

    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError as error:
        raise TranscriptionError(
            f"Input audio file does not exist: {candidate}."
        ) from error
    except (OSError, RuntimeError) as error:
        raise TranscriptionError(
            f"Could not safely resolve input audio {candidate}: {error}"
        ) from error

    if not is_e_drive_path(resolved):
        raise TranscriptionError(
            f"Input audio resolves outside /mnt/e: {candidate} -> {resolved}."
        )
    if not resolved.is_file():
        raise TranscriptionError(f"Input audio is not a regular file: {resolved}.")
    if resolved.suffix.casefold() not in SUPPORTED_AUDIO_EXTENSIONS:
        supported = ", ".join(sorted(SUPPORTED_AUDIO_EXTENSIONS))
        raise TranscriptionError(
            f"Unsupported audio extension {resolved.suffix!r}. Supported: {supported}."
        )

    try:
        input_size = resolved.stat().st_size
    except OSError as error:
        raise TranscriptionError(
            f"Could not inspect input audio {resolved}: {error}"
        ) from error
    if input_size == 0:
        raise TranscriptionError(f"Input audio file is empty: {resolved}.")
    if input_size > MAX_INPUT_BYTES:
        raise TranscriptionError(
            f"Input audio exceeds the 50 MiB limit: {resolved} "
            f"({input_size} bytes)."
        )
    return resolved


def validate_storage_directory(
    directory: os.PathLike[str] | str, label: str
) -> Path:
    """Resolve an existing or future directory without allowing E-drive escape."""

    candidate = Path(directory).expanduser()
    if not is_e_drive_path(candidate):
        raise TranscriptionError(f"{label} must be under /mnt/e: {candidate}.")
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise TranscriptionError(
            f"Could not safely resolve {label.casefold()} {candidate}: {error}"
        ) from error
    if not is_e_drive_path(resolved):
        raise TranscriptionError(
            f"{label} resolves outside /mnt/e: {candidate} -> {resolved}."
        )
    if resolved.exists() and not resolved.is_dir():
        raise TranscriptionError(f"{label} is not a directory: {resolved}.")
    return resolved


def _prepare_storage_directory(
    directory: os.PathLike[str] | str, label: str
) -> Path:
    resolved = validate_storage_directory(directory, label)
    try:
        resolved.mkdir(parents=True, exist_ok=True)
        final_path = resolved.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise TranscriptionError(
            f"Could not create {label.casefold()} {resolved}: {error}"
        ) from error
    if not is_e_drive_path(final_path) or not final_path.is_dir():
        raise TranscriptionError(
            f"{label} did not resolve to a directory under /mnt/e: {final_path}."
        )
    return final_path


def build_runtime_configuration(
    model_dir: os.PathLike[str] | str = DEFAULT_MODEL_DIR,
    cpu_threads: int = DEFAULT_CPU_THREADS,
) -> TranscriptionRuntimeConfiguration:
    """Build a validated model configuration without importing Faster-Whisper."""

    return TranscriptionRuntimeConfiguration(
        model_name=MODEL_NAME,
        device=DEVICE,
        compute_type=COMPUTE_TYPE,
        cpu_threads=validate_cpu_threads(cpu_threads),
        num_workers=NUM_WORKERS,
        model_dir=validate_storage_directory(model_dir, "Model directory"),
    )


def describe_transcription_configuration(
    output_dir: os.PathLike[str] | str = DEFAULT_TRANSCRIPT_OUTPUT_DIR,
    model_dir: os.PathLike[str] | str = DEFAULT_MODEL_DIR,
    cpu_threads: int = DEFAULT_CPU_THREADS,
) -> dict[str, object]:
    """Return dependency-free CLI configuration for inspection."""

    threads = validate_cpu_threads(cpu_threads)
    validate_storage_directory(output_dir, "Transcript output directory")
    validate_storage_directory(model_dir, "Model directory")
    return {
        "beam_size": BEAM_SIZE,
        "compute_type": COMPUTE_TYPE,
        "condition_on_previous_text": CONDITION_ON_PREVIOUS_TEXT,
        "cpu_threads": threads,
        "device": DEVICE,
        "language": LANGUAGE,
        "max_input_bytes": MAX_INPUT_BYTES,
        "model_dir": Path(model_dir).expanduser().as_posix(),
        "model_name": MODEL_NAME,
        "output_dir": Path(output_dir).expanduser().as_posix(),
        "supported_audio_extensions": sorted(SUPPORTED_AUDIO_EXTENSIONS),
        "vad_filter": VAD_FILTER,
        "workers": NUM_WORKERS,
    }


def _load_whisper_model_class() -> Any:
    try:
        from faster_whisper import WhisperModel
    except (ImportError, ModuleNotFoundError) as error:
        raise TranscriptionError(
            "Faster-Whisper is not installed. Activate the documented E-drive "
            "environment and install services/local_voice_api/requirements-stt.txt."
        ) from error
    return WhisperModel


def _ensure_whisper_model(
    configuration: TranscriptionRuntimeConfiguration,
    whisper_model_class: Any | None = None,
) -> Any:
    """Load the CPU model once and reject incompatible in-process reconfiguration."""

    global _loaded_runtime_config
    global _whisper_model

    if _runtime_shutdown:
        raise TranscriptionError(
            "The transcription runtime has been shut down. Start a fresh process "
            "before transcribing again."
        )
    if _whisper_model is not None:
        if configuration != _loaded_runtime_config:
            raise TranscriptionError(
                "Faster-Whisper is already loaded with different model directory "
                "or CPU thread settings. Start a fresh process to change them."
            )
        return _whisper_model

    model_class = (
        _load_whisper_model_class()
        if whisper_model_class is None
        else whisper_model_class
    )
    try:
        model = model_class(
            configuration.model_name,
            device=configuration.device,
            compute_type=configuration.compute_type,
            cpu_threads=configuration.cpu_threads,
            num_workers=configuration.num_workers,
            download_root=str(configuration.model_dir),
        )
    except Exception as error:
        raise TranscriptionError(
            f"Could not load Faster-Whisper model {configuration.model_name!r} "
            f"from {configuration.model_dir}. Model files may be missing, "
            f"incomplete, or unavailable for download: {error}"
        ) from error

    _whisper_model = model
    _loaded_runtime_config = configuration
    return _whisper_model


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as audio_file:
            for chunk in iter(lambda: audio_file.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise TranscriptionError(
            f"Could not read input audio {path} for hashing: {error}"
        ) from error
    return digest.hexdigest()


def _output_paths(
    audio_path: Path, audio_sha256: str, output_dir: Path
) -> tuple[Path, Path]:
    output_stem = f"{audio_path.stem}_transcript_{audio_sha256[:12]}"
    transcript_path = output_dir / f"{output_stem}.txt"
    metadata_path = output_dir / f"{output_stem}.json"
    for output_path in (transcript_path, metadata_path):
        if output_path.is_symlink():
            raise TranscriptionError(
                f"Transcript output path must not be a symlink: {output_path}."
            )
        if output_path.exists():
            raise TranscriptionError(
                f"Transcript output already exists; refusing to overwrite: {output_path}."
            )
    return transcript_path, metadata_path


def _finite_float(value: object, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a finite number.")
    try:
        converted = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} must be a finite number.") from error
    if not math.isfinite(converted):
        raise ValueError(f"{label} must be a finite number.")
    return converted


def _normalize_segments(raw_segments: list[Any]) -> tuple[TranscriptSegment, ...]:
    segments: list[TranscriptSegment] = []
    for index, raw_segment in enumerate(raw_segments):
        start = _finite_float(getattr(raw_segment, "start", None), "Segment start")
        end = _finite_float(getattr(raw_segment, "end", None), "Segment end")
        text = getattr(raw_segment, "text", None)
        if start < 0 or end < start:
            raise ValueError(f"Segment {index} has invalid timestamps {start} to {end}.")
        if not isinstance(text, str):
            raise ValueError(f"Segment {index} text must be a string.")
        normalized_text = text.strip()
        if normalized_text:
            segments.append(
                TranscriptSegment(start=start, end=end, text=normalized_text)
            )
    return tuple(segments)


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def transcribe_audio(
    audio_path: os.PathLike[str] | str,
    output_dir: os.PathLike[str] | str = DEFAULT_TRANSCRIPT_OUTPUT_DIR,
    model_dir: os.PathLike[str] | str = DEFAULT_MODEL_DIR,
    cpu_threads: int = DEFAULT_CPU_THREADS,
    *,
    whisper_model_class: Any | None = None,
) -> TranscriptionResult:
    """Transcribe one local file without uploading its audio or transcript."""

    threads = validate_cpu_threads(cpu_threads)
    resolved_audio = validate_audio_input(audio_path)
    resolved_output_dir = validate_storage_directory(
        output_dir, "Transcript output directory"
    )
    resolved_model_dir = validate_storage_directory(model_dir, "Model directory")
    resolved_output_dir = _prepare_storage_directory(
        resolved_output_dir, "Transcript output directory"
    )
    resolved_model_dir = _prepare_storage_directory(
        resolved_model_dir, "Model directory"
    )
    audio_sha256 = _sha256_file(resolved_audio)
    configuration = TranscriptionRuntimeConfiguration(
        model_name=MODEL_NAME,
        device=DEVICE,
        compute_type=COMPUTE_TYPE,
        cpu_threads=threads,
        num_workers=NUM_WORKERS,
        model_dir=resolved_model_dir,
    )

    with _runtime_lock:
        transcript_path, metadata_path = _output_paths(
            resolved_audio, audio_sha256, resolved_output_dir
        )
        model = _ensure_whisper_model(configuration, whisper_model_class)
        try:
            segment_generator, info = model.transcribe(
                str(resolved_audio),
                language=LANGUAGE,
                beam_size=BEAM_SIZE,
                vad_filter=VAD_FILTER,
                condition_on_previous_text=CONDITION_ON_PREVIOUS_TEXT,
            )
            raw_segments = list(segment_generator)
            segments = _normalize_segments(raw_segments)
            transcript = " ".join(segment.text for segment in segments).strip()
            if not transcript:
                raise ValueError(
                    "Faster-Whisper returned no speech; the audio may be empty or silent."
                )

            detected_language = getattr(info, "language", None)
            if not isinstance(detected_language, str) or not detected_language.strip():
                raise ValueError("Faster-Whisper returned no detected language.")
            detected_language = detected_language.strip()
            language_probability = _finite_float(
                getattr(info, "language_probability", None),
                "Language probability",
            )
            if not 0.0 <= language_probability <= 1.0:
                raise ValueError("Language probability must be between 0 and 1.")
            duration = _finite_float(getattr(info, "duration", None), "Duration")
            if duration < 0:
                raise ValueError("Duration must not be negative.")
        except Exception as error:
            raise TranscriptionError(
                f"Faster-Whisper transcription failed for {resolved_audio.name}: {error}"
            ) from error

        metadata = {
            "compute_type": configuration.compute_type,
            "cpu_threads": configuration.cpu_threads,
            "created_at_utc": _utc_timestamp(),
            "detected_language": detected_language,
            "device": configuration.device,
            "duration": duration,
            "language_probability": language_probability,
            "model_name": configuration.model_name,
            "segments": [
                {"end": segment.end, "start": segment.start, "text": segment.text}
                for segment in segments
            ],
            "source_audio_filename": resolved_audio.name,
            "source_audio_sha256": audio_sha256,
            "transcript": transcript,
        }
        created_paths: list[Path] = []
        try:
            with transcript_path.open(
                "x", encoding="utf-8", newline="\n"
            ) as transcript_file:
                created_paths.append(transcript_path)
                transcript_file.write(f"{transcript}\n")
            with metadata_path.open("x", encoding="utf-8", newline="\n") as file:
                created_paths.append(metadata_path)
                json.dump(
                    metadata,
                    file,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                file.write("\n")
        except FileExistsError as error:
            for created_path in created_paths:
                try:
                    created_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise TranscriptionError(
                "Transcript output appeared during transcription; refusing to "
                f"overwrite it: {error.filename}."
            ) from error
        except OSError as error:
            for created_path in created_paths:
                try:
                    created_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise TranscriptionError(
                f"Could not write local transcript outputs in {resolved_output_dir}: "
                f"{error}"
            ) from error

        return TranscriptionResult(
            transcript_path=transcript_path,
            metadata_path=metadata_path,
            transcript=transcript,
            detected_language=detected_language,
            language_probability=language_probability,
            duration=duration,
            segments=segments,
        )


def shutdown_transcription_runtime() -> None:
    """Permanently release the process-wide Faster-Whisper model reference."""

    global _loaded_runtime_config
    global _runtime_shutdown
    global _whisper_model

    with _runtime_lock:
        _whisper_model = None
        _loaded_runtime_config = None
        _runtime_shutdown = True
        gc.collect()
