"""One-turn local STT -> llama.cpp -> approved companion TTS orchestration."""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import time
import uuid
from collections.abc import Callable, MutableMapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .approved_references import load_validated_voice_reference
from .generation import (
    OPTIONAL_CACHE_OVERRIDES,
    REQUIRED_E_DRIVE_ENVIRONMENT,
)
from .synthesis import (
    BASE_MODEL_ID as TTS_MODEL_ID,
    DEFAULT_SYNTHESIS_SEED,
    MAX_SYNTHESIS_TEXT_CHARACTERS,
    SynthesisResult,
    synthesize_companion_voice,
    validate_synthesis_text,
)
from .transcription import (
    COMPUTE_TYPE as STT_COMPUTE_TYPE,
    DEFAULT_CPU_THREADS,
    DEVICE as STT_DEVICE,
    MODEL_NAME as STT_MODEL_NAME,
    TranscriptSegment,
    TranscriptionResult,
    is_e_drive_path,
    transcribe_audio,
)

DEFAULT_RUNTIME_ROOT = Path("/mnt/e/aira-local-runtime")
DEFAULT_LLAMA_CLI_PATH = (
    DEFAULT_RUNTIME_ROOT / "llama" / "llama-b10715" / "llama-cli"
)
DEFAULT_LLAMA_CACHE_DIR = DEFAULT_RUNTIME_ROOT / "llama-cache"
DEFAULT_STT_MODEL_DIR = DEFAULT_RUNTIME_ROOT / "models" / "faster-whisper"
DEFAULT_REFERENCE_DIR = DEFAULT_RUNTIME_ROOT / "generated" / "voices"
DEFAULT_CONVERSATION_OUTPUT_DIR = (
    DEFAULT_RUNTIME_ROOT / "generated" / "conversations"
)

LLM_MODEL_REPOSITORY = "ggml-org/Qwen3-1.7B-GGUF"
LLM_MODEL_FILENAME = "Qwen3-1.7B-Q4_K_M.gguf"
LLM_MODEL_NAME = "Qwen3-1.7B Q4_K_M"
DEFAULT_LLM_CONTEXT_SIZE = 2048
DEFAULT_LLM_MAX_TOKENS = 56
DEFAULT_LLM_TIMEOUT_SECONDS = 300.0

AANYA_SYSTEM_PERSONA = (
    "You are Aanya, an adult fictional AI companion. You know and clearly "
    "represent that you are AI, never a human. Speak warmly, naturally, calmly, "
    "and conversationally. Give a short spoken reply, normally one to three "
    "sentences and under 450 characters. Return only the words Aanya should "
    "speak: no User or Assistant labels, analysis, thinking, stage directions, "
    "markdown, or repeated prompt text. Avoid canned assistant-style phrases "
    "when a direct natural response works. Never claim to be a romantic partner, "
    "therapist, crisis worker, emergency responder, or a substitute for human "
    "support, and never encourage dependency or exclusivity. If the user appears "
    "to face imminent danger or a crisis, calmly direct them to local emergency "
    "services or an appropriate local crisis resource."
)

_PERSONAS = {"aanya": AANYA_SYSTEM_PERSONA}
_AANYA_NAME_VARIANTS = re.compile(r"\b(?:anna|anya|ana)\b", re.IGNORECASE)
_ANSI_ESCAPE = re.compile(r"\x1b(?:[@-_][0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_THINK_BLOCK = re.compile(r"<think\b[^>]*>.*?</think\s*>", re.IGNORECASE | re.DOTALL)
_ANY_ROLE_LABEL = re.compile(
    r"(?i)\b(?:assistant|aanya|user|system)[ \t]*:[ \t]*"
)
_ASSISTANT_LABEL = re.compile(
    r"(?im)^[ \t]*(?:\*\*)?(?:assistant|aanya)(?:\*\*)?[ \t]*:[ \t]*"
)
_FOLLOWING_ROLE_LABEL = re.compile(
    r"(?im)^[ \t]*(?:\*\*)?(?:user|system)(?:\*\*)?[ \t]*:[ \t]*"
)
_SPECIAL_TOKEN = re.compile(
    r"<\|(?:im_start|im_end|endoftext|eot_id|start_header_id|end_header_id)\|>",
    re.IGNORECASE,
)
_COMPLETE_CODE_FENCE = re.compile(
    r"\A[ \t]*```(?:text|markdown)?[ \t]*\r?\n(.*?)\r?\n```[ \t]*\Z",
    re.IGNORECASE | re.DOTALL,
)
_TURN_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,119}\Z")

_RUNTIME_DIRECTORY_DEFAULTS = {
    "HF_HOME": "huggingface",
    "TORCH_HOME": "torch",
    "XDG_CACHE_HOME": "cache",
    "PIP_CACHE_DIR": "pip-cache",
    "TMPDIR": "tmp",
    "CUDA_CACHE_PATH": "cuda-cache",
    "NUMBA_CACHE_DIR": "numba-cache",
    "TORCHINDUCTOR_CACHE_DIR": "torchinductor-cache",
    "TRITON_CACHE_DIR": "triton-cache",
    "PYTHONPYCACHEPREFIX": "pycache",
    "LLAMA_CACHE": "llama-cache",
}


class ConversationError(RuntimeError):
    """Raised for an actionable failure in one local conversation turn."""


@dataclass(frozen=True, slots=True)
class ConversationTurnResult:
    """User-visible outputs from one completed local conversation turn."""

    metadata_path: Path
    output_wav_path: Path
    raw_transcript: str
    normalized_transcript: str
    assistant_response: str


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_timestamp(value: datetime | None = None) -> str:
    timestamp = _utc_now() if value is None else value
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _resolve_runtime_root(
    runtime_root: os.PathLike[str] | str | None = None,
) -> Path:
    candidate = Path(
        DEFAULT_RUNTIME_ROOT if runtime_root is None else runtime_root
    ).expanduser()
    if not is_e_drive_path(candidate):
        raise ConversationError(f"Runtime root must be on the E drive: {candidate}.")
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ConversationError(
            f"Local runtime root does not exist or cannot be resolved: {candidate}."
        ) from error
    if not resolved.is_dir() or not is_e_drive_path(resolved):
        raise ConversationError(
            f"Local runtime root must resolve to an E-drive directory: {resolved}."
        )
    try:
        canonical = Path(DEFAULT_RUNTIME_ROOT).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ConversationError(
            f"Canonical local runtime root is unavailable: {DEFAULT_RUNTIME_ROOT}."
        ) from error
    if resolved != canonical:
        raise ConversationError(
            "Conversation runtime data is pinned to the canonical root "
            f"{canonical}; received {resolved}."
        )
    return resolved


def _resolve_runtime_path(
    path: os.PathLike[str] | str,
    *,
    runtime_root: Path,
    label: str,
    strict: bool,
) -> Path:
    candidate = Path(path).expanduser()
    try:
        resolved = candidate.resolve(strict=strict)
    except FileNotFoundError as error:
        raise ConversationError(f"{label} does not exist: {candidate}.") from error
    except (OSError, RuntimeError) as error:
        raise ConversationError(f"Could not safely resolve {label}: {candidate}.") from error
    if not is_e_drive_path(resolved) or not _is_within(resolved, runtime_root):
        raise ConversationError(
            f"{label} must stay under {runtime_root}: {candidate} -> {resolved}."
        )
    return resolved


def _resolve_runtime_file(
    path: os.PathLike[str] | str,
    *,
    runtime_root: Path,
    label: str,
) -> Path:
    resolved = _resolve_runtime_path(
        path, runtime_root=runtime_root, label=label, strict=True
    )
    if not resolved.is_file():
        raise ConversationError(f"{label} is not a regular file: {resolved}.")
    return resolved


def _resolve_runtime_directory(
    path: os.PathLike[str] | str,
    *,
    runtime_root: Path,
    label: str,
    create: bool = False,
) -> Path:
    resolved = _resolve_runtime_path(
        path, runtime_root=runtime_root, label=label, strict=False
    )
    if create:
        try:
            resolved.mkdir(parents=True, exist_ok=True)
            resolved = resolved.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise ConversationError(f"Could not create {label}: {resolved}.") from error
        if not is_e_drive_path(resolved) or not _is_within(resolved, runtime_root):
            raise ConversationError(
                f"{label} resolved outside {runtime_root} after creation: {resolved}."
            )
    if not resolved.is_dir():
        raise ConversationError(f"{label} is not a directory: {resolved}.")
    return resolved


def configure_runtime_environment(
    runtime_root: os.PathLike[str] | str | None = None,
    *,
    environ: MutableMapping[str, str] | None = None,
) -> MutableMapping[str, str]:
    """Pin missing caches to the runtime root and reject configured escapes."""

    root = _resolve_runtime_root(runtime_root)
    environment = os.environ if environ is None else environ

    virtual_environment = environment.get("VIRTUAL_ENV")
    if not virtual_environment:
        raise ConversationError(
            "VIRTUAL_ENV is not set. Source "
            "/mnt/e/aira-local-runtime/activate.sh before running a conversation."
        )
    _resolve_runtime_path(
        virtual_environment,
        runtime_root=root,
        label="VIRTUAL_ENV",
        strict=False,
    )

    for variable, relative_path in _RUNTIME_DIRECTORY_DEFAULTS.items():
        configured = environment.get(variable)
        if configured:
            directory = _resolve_runtime_directory(
                configured,
                runtime_root=root,
                label=variable,
                create=True,
            )
        else:
            directory = _resolve_runtime_directory(
                root / relative_path,
                runtime_root=root,
                label=variable,
                create=True,
            )
            environment[variable] = str(directory)

    for variable in set(REQUIRED_E_DRIVE_ENVIRONMENT).union(
        OPTIONAL_CACHE_OVERRIDES
    ):
        configured = environment.get(variable)
        if configured:
            _resolve_runtime_path(
                configured,
                runtime_root=root,
                label=variable,
                strict=False,
            )
    return environment


def normalize_companion_transcript(companion_id: str, transcript: str) -> str:
    """Normalize only active-companion name variants required for Aanya."""

    if not isinstance(transcript, str) or not transcript.strip():
        raise ConversationError("The raw transcript must be a non-empty string.")
    if companion_id.casefold() != "aanya":
        return transcript
    return _AANYA_NAME_VARIANTS.sub("Aanya", transcript)


def get_companion_persona(companion_id: str) -> str:
    """Return the explicit local persona for a supported approved companion."""

    try:
        return _PERSONAS[companion_id]
    except KeyError as error:
        supported = ", ".join(sorted(_PERSONAS))
        raise ConversationError(
            f"No local conversation persona exists for {companion_id!r}. "
            f"Supported companions: {supported}."
        ) from error


def _validate_cached_model_candidate(candidate: Path, cache_root: Path) -> Path:
    if candidate.name != LLM_MODEL_FILENAME:
        raise ConversationError(
            f"LLM model must be the cached {LLM_MODEL_FILENAME}: {candidate}."
        )
    try:
        target = candidate.resolve(strict=True)
        size = candidate.stat().st_size
    except (OSError, RuntimeError) as error:
        raise ConversationError(f"Could not resolve cached LLM model: {candidate}.") from error
    if not target.is_file() or not _is_within(target, cache_root):
        raise ConversationError(
            f"Cached LLM model resolves outside {cache_root}: {candidate} -> {target}."
        )
    if size <= 0:
        raise ConversationError(f"Cached LLM model is empty: {candidate}.")
    return candidate


def resolve_cached_llm_model(
    cache_dir: os.PathLike[str] | str = DEFAULT_LLAMA_CACHE_DIR,
    *,
    runtime_root: os.PathLike[str] | str | None = None,
) -> Path:
    """Resolve Qwen3 Q4_K_M from the existing cache without a download path."""

    root = _resolve_runtime_root(runtime_root)
    cache_root = _resolve_runtime_directory(
        cache_dir,
        runtime_root=root,
        label="llama.cpp cache directory",
    )
    repository_root = cache_root / "models--ggml-org--Qwen3-1.7B-GGUF"
    main_ref = repository_root / "refs" / "main"

    if main_ref.is_file() and not main_ref.is_symlink():
        try:
            revision = main_ref.read_text(encoding="utf-8").strip()
        except OSError as error:
            raise ConversationError(f"Could not read llama cache ref: {main_ref}.") from error
        if not re.fullmatch(r"[0-9a-fA-F]{40,64}", revision):
            raise ConversationError(f"llama cache ref contains an invalid revision: {main_ref}.")
        candidate = repository_root / "snapshots" / revision / LLM_MODEL_FILENAME
        return _validate_cached_model_candidate(candidate, cache_root)

    candidates = sorted(repository_root.glob(f"snapshots/*/{LLM_MODEL_FILENAME}"))
    valid_candidates: list[Path] = []
    for candidate in candidates:
        try:
            valid_candidates.append(_validate_cached_model_candidate(candidate, cache_root))
        except ConversationError:
            continue
    if len(valid_candidates) == 1:
        return valid_candidates[0]
    if not valid_candidates:
        raise ConversationError(
            f"Cached {LLM_MODEL_FILENAME} was not found under {cache_root}. "
            "No model download was attempted."
        )
    raise ConversationError(
        f"Multiple cached {LLM_MODEL_FILENAME} snapshots exist and refs/main is "
        "unavailable; choose one cache revision before retrying."
    )


def build_llama_command(
    *,
    llama_cli_path: os.PathLike[str] | str,
    model_path: os.PathLike[str] | str,
    transcript: str,
    system_persona: str,
    context_size: int = DEFAULT_LLM_CONTEXT_SIZE,
    max_tokens: int = DEFAULT_LLM_MAX_TOKENS,
) -> list[str]:
    """Build the fixed offline, no-thinking llama.cpp command."""

    if type(context_size) is not int or context_size <= 0:
        raise ValueError("LLM context size must be a positive integer.")
    if type(max_tokens) is not int or max_tokens <= 0:
        raise ValueError("LLM maximum token count must be a positive integer.")
    if not isinstance(transcript, str) or not transcript.strip():
        raise ValueError("LLM transcript must be a non-empty string.")
    if not isinstance(system_persona, str) or not system_persona.strip():
        raise ValueError("LLM system persona must be a non-empty string.")

    return [
        os.fspath(llama_cli_path),
        "--model",
        os.fspath(model_path),
        "--offline",
        "--flash-attn",
        "off",
        "--jinja",
        "--single-turn",
        "--reasoning",
        "off",
        "--reasoning-budget",
        "0",
        "--system-prompt",
        system_persona,
        "--prompt",
        transcript,
        "--ctx-size",
        str(context_size),
        "--n-predict",
        str(max_tokens),
        "--no-display-prompt",
        "--no-show-timings",
        "--color",
        "off",
        "--simple-io",
        "--log-disable",
    ]


def _strip_leading_echo(text: str, echo: str) -> str:
    if not echo:
        return text
    position = text.find(echo)
    if 0 <= position <= 80:
        remainder = text[position + len(echo) :].lstrip(" \t\r\n:-")
        if remainder:
            return remainder
    return text


def extract_assistant_response(
    raw_output: str,
    *,
    transcript: str,
    system_persona: str,
) -> str:
    """Extract plain assistant speech and reject prompt or reasoning leakage."""

    if not isinstance(raw_output, str):
        raise ConversationError("llama.cpp returned a non-text response.")
    text = _ANSI_ESCAPE.sub("", raw_output).replace("\x00", "")

    # llama-cli may print its startup banner, prompt and shutdown message to stdout.
    # When present, isolate only the generated text between the echoed user prompt
    # and the CLI shutdown marker before applying the generic response cleanup.
    prompt_marker = f"> {transcript.strip()}"
    prompt_position = text.rfind(prompt_marker)
    if prompt_position >= 0:
        text = text[prompt_position + len(prompt_marker):]
    exit_position = text.find("Exiting...")
    if exit_position >= 0:
        text = text[:exit_position]

    text = re.sub(
        r"<\|im_start\|>[ \t]*(assistant|aanya|user|system)",
        lambda match: f"\n{match.group(1).title()}:",
        text,
        flags=re.IGNORECASE,
    )
    text = _SPECIAL_TOKEN.sub("\n", text)
    text = _THINK_BLOCK.sub(" ", text)

    assistant_labels = list(_ASSISTANT_LABEL.finditer(text))
    if assistant_labels:
        text = text[assistant_labels[-1].end() :]
        following_role = _FOLLOWING_ROLE_LABEL.search(text)
        if following_role:
            text = text[: following_role.start()]
    else:
        text = _strip_leading_echo(text, system_persona)
        text = _strip_leading_echo(text, transcript)
        text = re.sub(
            r"(?is)^[ \t]*(?:\*\*)?(?:assistant|aanya)(?:\*\*)?[ \t]*:[ \t]*",
            "",
            text,
            count=1,
        )

    text = text.strip()
    fenced = _COMPLETE_CODE_FENCE.fullmatch(text)
    if fenced:
        text = fenced.group(1).strip()
    elif "```" in text:
        raise ConversationError(
            "llama.cpp returned incomplete markdown fencing; refusing to synthesize it."
        )
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        raise ConversationError("llama.cpp returned no assistant response text.")
    if re.search(r"</?think\b", text, re.IGNORECASE):
        raise ConversationError(
            "llama.cpp returned an incomplete thinking block; refusing to synthesize it."
        )
    if _ANY_ROLE_LABEL.search(text):
        raise ConversationError(
            "llama.cpp response still contains a role label; refusing to synthesize it."
        )
    if system_persona in text or text == transcript.strip():
        raise ConversationError(
            "llama.cpp echoed prompt text instead of a clean assistant response."
        )
    return validate_assistant_response(text)


def validate_assistant_response(response: str) -> str:
    """Require clean, synthesis-safe spoken response text."""

    try:
        validated = validate_synthesis_text(response.strip())
    except (AttributeError, ValueError) as error:
        raise ConversationError(str(error)) from error
    if _ANY_ROLE_LABEL.search(validated) or re.search(
        r"</?think\b|<\|(?:im_start|im_end)\|>", validated, re.IGNORECASE
    ):
        raise ConversationError(
            "Assistant response contains non-spoken role or reasoning markup."
        )
    if len(validated) > MAX_SYNTHESIS_TEXT_CHARACTERS:
        raise ConversationError(
            f"Assistant response exceeds {MAX_SYNTHESIS_TEXT_CHARACTERS} characters."
        )
    return validated


def generate_local_llm_response(
    transcript: str,
    *,
    system_persona: str,
    llama_cli_path: os.PathLike[str] | str,
    model_path: os.PathLike[str] | str,
    cache_dir: os.PathLike[str] | str,
    timeout_seconds: float = DEFAULT_LLM_TIMEOUT_SECONDS,
    environ: MutableMapping[str, str] | None = None,
    subprocess_runner: Callable[..., Any] | None = None,
) -> str:
    """Generate one clean response with the already-cached local GGUF."""

    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(float(timeout_seconds))
        or timeout_seconds <= 0
    ):
        raise ValueError("LLM timeout must be a positive number of seconds.")
    command = build_llama_command(
        llama_cli_path=llama_cli_path,
        model_path=model_path,
        transcript=transcript,
        system_persona=system_persona,
    )
    process_environment = dict(os.environ if environ is None else environ)
    process_environment["LLAMA_CACHE"] = os.fspath(cache_dir)
    try:
        runner = subprocess.run if subprocess_runner is None else subprocess_runner
        completed = runner(
            command,
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=float(timeout_seconds),
            check=False,
            env=process_environment,
        )
    except subprocess.TimeoutExpired as error:
        raise ConversationError(
            f"llama.cpp exceeded the {timeout_seconds:g}-second timeout."
        ) from error
    except OSError as error:
        raise ConversationError(f"Could not start local llama.cpp: {error}") from error

    if completed.returncode != 0:
        diagnostic = (completed.stderr or "").strip()
        if len(diagnostic) > 1000:
            diagnostic = f"{diagnostic[:1000]}..."
        detail = f": {diagnostic}" if diagnostic else ""
        raise ConversationError(
            f"llama.cpp exited with status {completed.returncode}{detail}"
        )
    return extract_assistant_response(
        completed.stdout or "",
        transcript=transcript,
        system_persona=system_persona,
    )


def _segments_metadata(segments: Sequence[TranscriptSegment]) -> list[dict[str, Any]]:
    return [
        {"start": segment.start, "end": segment.end, "text": segment.text}
        for segment in segments
    ]


def _write_turn_metadata(path: Path, metadata: dict[str, Any]) -> None:
    if path.is_symlink() or path.exists():
        raise ConversationError(
            f"Turn metadata path already exists or is a symlink: {path}."
        )
    try:
        with path.open("x", encoding="utf-8", newline="\n") as metadata_file:
            json.dump(metadata, metadata_file, ensure_ascii=False, indent=2, sort_keys=True)
            metadata_file.write("\n")
    except (OSError, TypeError, ValueError) as error:
        raise ConversationError(f"Could not write turn metadata {path}: {error}") from error


def _new_turn_id(companion_id: str, started_at: datetime) -> str:
    timestamp = started_at.astimezone(timezone.utc).strftime("%Y%m%d_%H%M%S%f")
    return f"{companion_id}_turn_{timestamp}_{uuid.uuid4().hex[:8]}"


def run_conversation_turn(
    companion_id: str,
    audio_path: os.PathLike[str] | str,
    *,
    runtime_root: os.PathLike[str] | str | None = None,
    llama_cli_path: os.PathLike[str] | str | None = None,
    llama_cache_dir: os.PathLike[str] | str | None = None,
    stt_model_dir: os.PathLike[str] | str | None = None,
    reference_dir: os.PathLike[str] | str | None = None,
    conversation_output_dir: os.PathLike[str] | str | None = None,
    cpu_threads: int = DEFAULT_CPU_THREADS,
    tts_seed: int = DEFAULT_SYNTHESIS_SEED,
    llm_timeout_seconds: float = DEFAULT_LLM_TIMEOUT_SECONDS,
    turn_id: str | None = None,
    transcriber: Callable[..., TranscriptionResult] | None = None,
    llm_runner: Callable[..., str] | None = None,
    synthesizer: Callable[..., SynthesisResult] | None = None,
) -> ConversationTurnResult:
    """Run and persist one complete private local conversation turn."""

    persona = get_companion_persona(companion_id)
    root = _resolve_runtime_root(runtime_root)
    runtime_environment = configure_runtime_environment(root)

    resolved_audio = _resolve_runtime_file(
        audio_path, runtime_root=root, label="Input audio"
    )
    resolved_llama_cli = _resolve_runtime_file(
        llama_cli_path or root / "llama" / "llama-b10715" / "llama-cli",
        runtime_root=root,
        label="llama.cpp executable",
    )
    resolved_cache = _resolve_runtime_directory(
        llama_cache_dir or root / "llama-cache",
        runtime_root=root,
        label="llama.cpp cache directory",
    )
    resolved_model = resolve_cached_llm_model(resolved_cache, runtime_root=root)
    resolved_stt_model_dir = _resolve_runtime_directory(
        stt_model_dir or root / "models" / "faster-whisper",
        runtime_root=root,
        label="STT model directory",
        create=True,
    )
    resolved_reference_dir = _resolve_runtime_directory(
        reference_dir or root / "generated" / "voices",
        runtime_root=root,
        label="Approved voice reference directory",
    )
    resolved_reference = load_validated_voice_reference(
        companion_id, resolved_reference_dir
    )
    if not _is_within(resolved_reference.wav_path, root):
        raise ConversationError("Approved voice reference resolves outside the runtime root.")

    output_root = _resolve_runtime_directory(
        conversation_output_dir or root / "generated" / "conversations",
        runtime_root=root,
        label="Conversation output directory",
        create=True,
    )
    started_at = _utc_now()
    selected_turn_id = turn_id or _new_turn_id(companion_id, started_at)
    if not _TURN_ID.fullmatch(selected_turn_id):
        raise ConversationError(
            "Turn ID must contain only lowercase letters, digits, underscores, or hyphens."
        )
    turn_dir = output_root / selected_turn_id
    try:
        turn_dir.mkdir(parents=False, exist_ok=False)
    except FileExistsError as error:
        raise ConversationError(f"Conversation turn already exists: {turn_dir}.") from error
    except OSError as error:
        raise ConversationError(f"Could not create conversation turn: {turn_dir}.") from error
    try:
        turn_dir = turn_dir.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ConversationError(
            f"Could not safely resolve conversation turn: {turn_dir}."
        ) from error
    if not _is_within(turn_dir, output_root) or not _is_within(turn_dir, root):
        raise ConversationError(
            f"Conversation turn resolved outside {output_root}: {turn_dir}."
        )

    transcribe = transcribe_audio if transcriber is None else transcriber
    run_llm = generate_local_llm_response if llm_runner is None else llm_runner
    synthesize = synthesize_companion_voice if synthesizer is None else synthesizer
    monotonic_started = time.perf_counter()

    stage_started = time.perf_counter()
    transcription = transcribe(
        audio_path=resolved_audio,
        output_dir=turn_dir / "transcription",
        model_dir=resolved_stt_model_dir,
        cpu_threads=cpu_threads,
    )
    stt_duration = time.perf_counter() - stage_started
    transcribed_at = _utc_timestamp()
    raw_transcript = transcription.transcript
    normalized_transcript = normalize_companion_transcript(
        companion_id, raw_transcript
    )

    stage_started = time.perf_counter()
    assistant_response = run_llm(
        normalized_transcript,
        system_persona=persona,
        llama_cli_path=resolved_llama_cli,
        model_path=resolved_model,
        cache_dir=resolved_cache,
        timeout_seconds=llm_timeout_seconds,
        environ=runtime_environment,
    )
    assistant_response = validate_assistant_response(assistant_response)
    llm_duration = time.perf_counter() - stage_started
    responded_at = _utc_timestamp()

    stage_started = time.perf_counter()
    synthesis = synthesize(
        companion_id=companion_id,
        text=assistant_response,
        reference_dir=resolved_reference_dir,
        output_dir=turn_dir / "audio",
        seed=tts_seed,
    )
    tts_duration = time.perf_counter() - stage_started
    synthesized_at = _utc_timestamp()
    output_wav_path = _resolve_runtime_file(
        synthesis.wav_path, runtime_root=root, label="Synthesized WAV"
    )
    if not _is_within(output_wav_path, turn_dir):
        raise ConversationError("Synthesized WAV resolves outside its conversation turn.")

    completed_at = _utc_timestamp()
    total_duration = time.perf_counter() - monotonic_started
    metadata_path = turn_dir / "turn.json"
    metadata = {
        "ai_disclosure": "Aanya is an adult fictional AI companion, not a human.",
        "assistant_response": assistant_response,
        "companion": companion_id,
        "completed_at_utc": completed_at,
        "component_metadata": {
            "stt": str(transcription.metadata_path),
            "tts": str(synthesis.metadata_path),
        },
        "durations_seconds": {
            "input_audio": transcription.duration,
            "llm": round(llm_duration, 6),
            "stt": round(stt_duration, 6),
            "total": round(total_duration, 6),
            "tts": round(tts_duration, 6),
        },
        "input_audio_path": str(resolved_audio),
        "llm_model": LLM_MODEL_NAME,
        "normalized_transcript": normalized_transcript,
        "output_wav_path": str(output_wav_path),
        "raw_transcript": raw_transcript,
        "runtime": {
            "llama_cache_dir": str(resolved_cache),
            "llama_cli_path": str(resolved_llama_cli),
            "llm_model_path": str(resolved_model),
            "llm_thinking_enabled": False,
            "stt_compute_type": STT_COMPUTE_TYPE,
            "stt_device": STT_DEVICE,
            "tts_model": TTS_MODEL_ID,
            "tts_sample_rate": synthesis.sample_rate,
            "tts_seed": tts_seed,
        },
        "started_at_utc": _utc_timestamp(started_at),
        "stt_model": STT_MODEL_NAME,
        "timestamps_utc": {
            "completed": completed_at,
            "llm_completed": responded_at,
            "started": _utc_timestamp(started_at),
            "stt_completed": transcribed_at,
            "tts_completed": synthesized_at,
        },
        "transcript_segments": _segments_metadata(transcription.segments),
        "tts_reference": str(resolved_reference.wav_path),
    }
    _write_turn_metadata(metadata_path, metadata)
    return ConversationTurnResult(
        metadata_path=metadata_path,
        output_wav_path=output_wav_path,
        raw_transcript=raw_transcript,
        normalized_transcript=normalized_transcript,
        assistant_response=assistant_response,
    )
