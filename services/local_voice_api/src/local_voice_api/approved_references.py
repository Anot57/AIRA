"""Approved local voice references and dependency-free provenance checks."""

from __future__ import annotations

import json
import wave
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from .generation import MODEL_ID as VOICE_DESIGN_MODEL_ID
from .profiles import EXPECTED_COMPANION_IDS, SERVICE_ROOT

MANIFEST_PATH = SERVICE_ROOT / "config" / "approved_voice_references.json"
MANIFEST_FIELDS = frozenset(
    {"companion_id", "seed", "wav_filename", "metadata_filename"}
)
MAX_REFERENCE_SAMPLE_RATE = 384_000


class ApprovedReferenceValidationError(ValueError):
    """Raised when approved reference configuration or files are invalid."""


@dataclass(frozen=True, slots=True)
class ApprovedVoiceReference:
    """Machine-independent approval record for one companion voice."""

    companion_id: str
    seed: int
    wav_filename: str
    metadata_filename: str


@dataclass(frozen=True, slots=True)
class ResolvedVoiceReference:
    """Exact local files selected from an approval record."""

    approved: ApprovedVoiceReference
    reference_dir: Path
    wav_path: Path
    metadata_path: Path


@dataclass(frozen=True, slots=True)
class ValidatedVoiceReference:
    """An approved reference whose metadata and WAV have passed validation."""

    approved: ApprovedVoiceReference
    wav_path: Path
    metadata_path: Path
    transcript: str
    sample_rate: int

    def cache_signature(self) -> tuple[object, ...]:
        """Return a process-local identity used to invalidate stale prompts."""

        wav_stat = self.wav_path.stat()
        metadata_stat = self.metadata_path.stat()
        return (
            self.approved.companion_id,
            self.approved.seed,
            self.wav_path,
            wav_stat.st_size,
            wav_stat.st_mtime_ns,
            self.metadata_path,
            metadata_stat.st_size,
            metadata_stat.st_mtime_ns,
            self.transcript,
        )


def _require_manifest_text(
    raw_reference: Mapping[str, Any], field: str, index: int
) -> str:
    value = raw_reference[field]
    if not isinstance(value, str) or not value.strip():
        raise ApprovedReferenceValidationError(
            f"Approval {index} field {field!r} must be a non-empty string."
        )
    if value != value.strip():
        raise ApprovedReferenceValidationError(
            f"Approval {index} field {field!r} has surrounding whitespace."
        )
    return value


def _validate_filename(filename: str, expected: str, field: str) -> None:
    if (
        filename in {".", ".."}
        or "/" in filename
        or "\\" in filename
        or ":" in filename
        or "\x00" in filename
    ):
        raise ApprovedReferenceValidationError(
            f"Approval field {field!r} must contain a relative basename only."
        )
    if filename != expected:
        raise ApprovedReferenceValidationError(
            f"Approval field {field!r} must be the canonical filename {expected!r}."
        )


def validate_approved_voice_references(
    raw_references: object,
) -> tuple[ApprovedVoiceReference, ...]:
    """Validate raw approval records without inspecting runtime files."""

    if not isinstance(raw_references, list) or not raw_references:
        raise ApprovedReferenceValidationError(
            "Approved voice reference manifest must be a non-empty list."
        )

    approvals: list[ApprovedVoiceReference] = []
    seen_companions: set[str] = set()
    seen_filenames: set[str] = set()

    for index, raw_reference in enumerate(raw_references):
        if not isinstance(raw_reference, dict):
            raise ApprovedReferenceValidationError(
                f"Approval {index} must be an object."
            )

        fields = set(raw_reference)
        missing = sorted(MANIFEST_FIELDS - fields)
        unexpected = sorted(fields - MANIFEST_FIELDS)
        if missing or unexpected:
            raise ApprovedReferenceValidationError(
                f"Approval {index} has missing fields {missing} and unexpected "
                f"fields {unexpected}."
            )

        companion_id = _require_manifest_text(
            raw_reference, "companion_id", index
        )
        if companion_id not in EXPECTED_COMPANION_IDS:
            raise ApprovedReferenceValidationError(
                f"Approval {index} has unknown companion ID {companion_id!r}."
            )
        if companion_id in seen_companions:
            raise ApprovedReferenceValidationError(
                f"Companion {companion_id!r} is approved more than once."
            )

        seed = raw_reference["seed"]
        if type(seed) is not int or seed < 0:
            raise ApprovedReferenceValidationError(
                f"Approval {companion_id!r} seed must be a non-negative integer."
            )

        wav_filename = _require_manifest_text(
            raw_reference, "wav_filename", index
        )
        metadata_filename = _require_manifest_text(
            raw_reference, "metadata_filename", index
        )
        _validate_filename(
            wav_filename,
            f"{companion_id}_seed_{seed}.wav",
            "wav_filename",
        )
        _validate_filename(
            metadata_filename,
            f"{companion_id}_seed_{seed}.json",
            "metadata_filename",
        )

        for filename in (wav_filename, metadata_filename):
            normalised_filename = filename.casefold()
            if normalised_filename in seen_filenames:
                raise ApprovedReferenceValidationError(
                    f"Approved filename {filename!r} is duplicated."
                )
            seen_filenames.add(normalised_filename)

        approvals.append(
            ApprovedVoiceReference(
                companion_id=companion_id,
                seed=seed,
                wav_filename=wav_filename,
                metadata_filename=metadata_filename,
            )
        )
        seen_companions.add(companion_id)

    return tuple(approvals)


def load_approved_voice_references(
    path: Path = MANIFEST_PATH,
) -> tuple[ApprovedVoiceReference, ...]:
    """Load the checked-in approval manifest."""

    try:
        with path.open(encoding="utf-8") as manifest_file:
            raw_references = json.load(manifest_file)
    except (OSError, json.JSONDecodeError) as error:
        raise ApprovedReferenceValidationError(
            f"Could not load approved voice references from {path}: {error}"
        ) from error
    return validate_approved_voice_references(raw_references)


APPROVED_VOICE_REFERENCES = load_approved_voice_references()
APPROVED_VOICE_REFERENCES_BY_ID: Mapping[str, ApprovedVoiceReference] = (
    MappingProxyType(
        {
            reference.companion_id: reference
            for reference in APPROVED_VOICE_REFERENCES
        }
    )
)


def get_approved_voice_reference(companion_id: str) -> ApprovedVoiceReference:
    """Return the explicit approval for a companion; never infer a file."""

    try:
        return APPROVED_VOICE_REFERENCES_BY_ID[companion_id]
    except KeyError as error:
        approved_ids = ", ".join(APPROVED_VOICE_REFERENCES_BY_ID)
        raise ApprovedReferenceValidationError(
            f"Companion {companion_id!r} has no approved voice reference. "
            f"Approved IDs: {approved_ids}."
        ) from error


def _resolve_exact_file(reference_dir: Path, filename: str) -> Path:
    unresolved_path = reference_dir / filename
    if unresolved_path.is_symlink():
        raise ApprovedReferenceValidationError(
            f"Approved reference file must not be a symlink: {unresolved_path}."
        )

    resolved_path = unresolved_path.resolve(strict=False)
    if resolved_path.parent != reference_dir or resolved_path.name != filename:
        raise ApprovedReferenceValidationError(
            f"Approved reference path escapes its reference directory: {filename!r}."
        )
    if not resolved_path.is_file():
        raise ApprovedReferenceValidationError(
            f"Approved reference file does not exist: {resolved_path}."
        )
    return resolved_path


def resolve_approved_voice_reference(
    companion_id: str, reference_dir: Path | str
) -> ResolvedVoiceReference:
    """Resolve only the filenames named by the manifest, with no fallback."""

    approved = get_approved_voice_reference(companion_id)
    resolved_dir = Path(reference_dir).expanduser().resolve(strict=False)
    if not resolved_dir.is_dir():
        raise ApprovedReferenceValidationError(
            f"Reference directory does not exist: {resolved_dir}."
        )

    wav_path = _resolve_exact_file(resolved_dir, approved.wav_filename)
    metadata_path = _resolve_exact_file(
        resolved_dir, approved.metadata_filename
    )
    return ResolvedVoiceReference(
        approved=approved,
        reference_dir=resolved_dir,
        wav_path=wav_path,
        metadata_path=metadata_path,
    )


def _validate_reference_wav(wav_path: Path, sample_rate: int) -> None:
    try:
        with wave.open(str(wav_path), "rb") as wav_file:
            wav_sample_rate = wav_file.getframerate()
            channels = wav_file.getnchannels()
            sample_width = wav_file.getsampwidth()
            frame_count = wav_file.getnframes()
    except (OSError, EOFError, wave.Error) as error:
        raise ApprovedReferenceValidationError(
            f"Approved WAV is unreadable or invalid: {wav_path}."
        ) from error

    if channels < 1 or sample_width < 1 or frame_count < 1:
        raise ApprovedReferenceValidationError(
            f"Approved WAV contains no valid audio frames: {wav_path}."
        )
    if wav_sample_rate != sample_rate:
        raise ApprovedReferenceValidationError(
            "Approved WAV sample rate does not match its metadata: "
            f"{wav_sample_rate} != {sample_rate}."
        )


def validate_reference_metadata(
    resolved: ResolvedVoiceReference, raw_metadata: object
) -> ValidatedVoiceReference:
    """Validate approved reference provenance, safety flags, transcript, and WAV."""

    approved = resolved.approved
    if resolved.wav_path.name != approved.wav_filename:
        raise ApprovedReferenceValidationError(
            "Resolved WAV filename does not match the approval manifest."
        )
    if resolved.metadata_path.name != approved.metadata_filename:
        raise ApprovedReferenceValidationError(
            "Resolved metadata filename does not match the approval manifest."
        )
    if not isinstance(raw_metadata, dict):
        raise ApprovedReferenceValidationError(
            "Approved reference metadata must be a JSON object."
        )

    if raw_metadata.get("companion_id") != approved.companion_id:
        raise ApprovedReferenceValidationError(
            "Reference metadata companion ID does not match its approval."
        )
    metadata_seed = raw_metadata.get("seed")
    if type(metadata_seed) is not int or metadata_seed != approved.seed:
        raise ApprovedReferenceValidationError(
            "Reference metadata seed does not match its approval."
        )
    if raw_metadata.get("adult") is not True:
        raise ApprovedReferenceValidationError(
            "Reference metadata must explicitly mark the voice as adult."
        )
    if raw_metadata.get("ai_generated") is not True:
        raise ApprovedReferenceValidationError(
            "Reference metadata must explicitly mark the voice as AI-generated."
        )

    transcript = raw_metadata.get("transcript")
    if not isinstance(transcript, str) or not transcript.strip():
        raise ApprovedReferenceValidationError(
            "Reference metadata transcript must be a non-empty string."
        )
    if transcript != transcript.strip():
        raise ApprovedReferenceValidationError(
            "Reference metadata transcript has surrounding whitespace."
        )
    if raw_metadata.get("model_id") != VOICE_DESIGN_MODEL_ID:
        raise ApprovedReferenceValidationError(
            "Reference metadata must identify the approved VoiceDesign model."
        )

    sample_rate = raw_metadata.get("sample_rate")
    if (
        type(sample_rate) is not int
        or sample_rate <= 0
        or sample_rate > MAX_REFERENCE_SAMPLE_RATE
    ):
        raise ApprovedReferenceValidationError(
            "Reference metadata sample rate must be a valid positive integer."
        )

    optional_filename_fields = {
        "wav_filename": approved.wav_filename,
        "metadata_filename": approved.metadata_filename,
    }
    for field, expected_filename in optional_filename_fields.items():
        if field in raw_metadata and raw_metadata[field] != expected_filename:
            raise ApprovedReferenceValidationError(
                f"Reference metadata field {field!r} does not match its approval."
            )

    _validate_reference_wav(resolved.wav_path, sample_rate)
    return ValidatedVoiceReference(
        approved=approved,
        wav_path=resolved.wav_path,
        metadata_path=resolved.metadata_path,
        transcript=transcript,
        sample_rate=sample_rate,
    )


def load_validated_voice_reference(
    companion_id: str, reference_dir: Path | str
) -> ValidatedVoiceReference:
    """Resolve and validate the exact approved local reference."""

    resolved = resolve_approved_voice_reference(companion_id, reference_dir)
    try:
        with resolved.metadata_path.open(encoding="utf-8") as metadata_file:
            raw_metadata = json.load(metadata_file)
    except (OSError, json.JSONDecodeError) as error:
        raise ApprovedReferenceValidationError(
            f"Could not load approved metadata {resolved.metadata_path}: {error}"
        ) from error
    return validate_reference_metadata(resolved, raw_metadata)

