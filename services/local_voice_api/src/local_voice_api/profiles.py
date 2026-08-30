"""Validated, dependency-free configuration for Local Voice Lab profiles."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

EXPECTED_COMPANION_IDS = (
    "aanya",
    "tara",
    "riya",
    "kavya",
    "naina",
    "isha",
    "sana",
    "diya",
    "anika",
    "meera",
    "zara",
    "priya",
    "leela",
    "maya",
    "simran",
    "noor",
    "avni",
    "myra",
    "saanvi",
    "rhea",
)

SERVICE_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = SERVICE_ROOT / "config" / "voice_profiles.json"

PROFILE_FIELDS = frozenset(
    {
        "companion_id",
        "language",
        "english_style",
        "timbre",
        "pace",
        "energy",
        "emotional_style",
        "speaking_characteristics",
        "voice_description",
        "reference_transcript",
        "ai_generated",
        "adult",
    }
)

VOICE_DIMENSION_FIELDS = (
    "english_style",
    "timbre",
    "pace",
    "energy",
    "emotional_style",
    "speaking_characteristics",
)

FORBIDDEN_VOICE_WORDING = (
    "celebrity",
    "real person",
    "real-person",
    "public figure",
    "famous person",
    "sounds like",
    "sound like",
    "imitate",
    "imitation",
    "impersonate",
    "impersonation",
    "in the style of",
    "voice of",
    "modeled after",
    "modelled after",
    "based on",
    "replica of",
    "recreation of",
    "copy of",
    "clone of",
    "resemble",
    "resembling",
    "inspired by",
    "seductive",
    "sultry",
    "sexy",
    "childlike",
    "child-like",
    "child voice",
    "little girl",
    "young girl",
    "teenage",
    "teenager",
    "juvenile",
    "exaggerated",
)

_TEXT_FIELDS = (
    "language",
    *VOICE_DIMENSION_FIELDS,
    "voice_description",
    "reference_transcript",
)
_REQUIRED_DESCRIPTION_WORDING = (
    "original",
    "fictional",
    "adult",
    "ai generated",
    "english first",
)


class VoiceProfileValidationError(ValueError):
    """Raised when the checked-in voice profile configuration is unsafe or invalid."""


@dataclass(frozen=True, slots=True)
class VoiceProfile:
    """A single original adult AI voice design."""

    companion_id: str
    language: str
    english_style: str
    timbre: str
    pace: str
    energy: str
    emotional_style: str
    speaking_characteristics: str
    voice_description: str
    reference_transcript: str
    ai_generated: bool
    adult: bool


def _normalise_wording(value: str) -> str:
    return " ".join(value.casefold().replace("-", " ").split())


def _require_text(raw_profile: Mapping[str, Any], field: str, index: int) -> str:
    value = raw_profile[field]
    if not isinstance(value, str) or not value.strip():
        raise VoiceProfileValidationError(
            f"Profile {index} field {field!r} must be a non-empty string."
        )
    if value != value.strip():
        raise VoiceProfileValidationError(
            f"Profile {index} field {field!r} has surrounding whitespace."
        )
    return value


def validate_voice_profiles(raw_profiles: object) -> tuple[VoiceProfile, ...]:
    """Validate raw profile data and return immutable typed profiles."""

    if not isinstance(raw_profiles, list):
        raise VoiceProfileValidationError("Voice profile configuration must be a list.")

    profiles: list[VoiceProfile] = []
    for index, raw_profile in enumerate(raw_profiles):
        if not isinstance(raw_profile, dict):
            raise VoiceProfileValidationError(f"Profile {index} must be an object.")

        fields = set(raw_profile)
        missing = sorted(PROFILE_FIELDS - fields)
        unexpected = sorted(fields - PROFILE_FIELDS)
        if missing or unexpected:
            raise VoiceProfileValidationError(
                f"Profile {index} has missing fields {missing} and unexpected fields "
                f"{unexpected}."
            )

        companion_id = _require_text(raw_profile, "companion_id", index)
        text_values = {
            field: _require_text(raw_profile, field, index) for field in _TEXT_FIELDS
        }

        if text_values["language"] != "English":
            raise VoiceProfileValidationError(
                f"Profile {companion_id!r} must use English as its primary language."
            )
        if raw_profile["adult"] is not True:
            raise VoiceProfileValidationError(
                f"Profile {companion_id!r} must be explicitly adult."
            )
        if raw_profile["ai_generated"] is not True:
            raise VoiceProfileValidationError(
                f"Profile {companion_id!r} must be explicitly AI-generated."
            )

        description = _normalise_wording(text_values["voice_description"])
        for required_wording in _REQUIRED_DESCRIPTION_WORDING:
            if required_wording not in description:
                raise VoiceProfileValidationError(
                    f"Profile {companion_id!r} description is missing required wording "
                    f"{required_wording!r}."
                )

        all_profile_wording = _normalise_wording(
            " ".join(text_values[field] for field in _TEXT_FIELDS)
        )
        for forbidden_wording in FORBIDDEN_VOICE_WORDING:
            if _normalise_wording(forbidden_wording) in all_profile_wording:
                raise VoiceProfileValidationError(
                    f"Profile {companion_id!r} contains forbidden voice wording."
                )

        ai_disclosure = re.search(
            r"\bAI\b", text_values["reference_transcript"], re.IGNORECASE
        )
        if ai_disclosure is None:
            raise VoiceProfileValidationError(
                f"Profile {companion_id!r} reference transcript must disclose "
                "AI identity."
            )

        profiles.append(
            VoiceProfile(
                companion_id=companion_id,
                language=text_values["language"],
                english_style=text_values["english_style"],
                timbre=text_values["timbre"],
                pace=text_values["pace"],
                energy=text_values["energy"],
                emotional_style=text_values["emotional_style"],
                speaking_characteristics=text_values["speaking_characteristics"],
                voice_description=text_values["voice_description"],
                reference_transcript=text_values["reference_transcript"],
                ai_generated=True,
                adult=True,
            )
        )

    actual_ids = tuple(profile.companion_id for profile in profiles)
    if actual_ids != EXPECTED_COMPANION_IDS:
        raise VoiceProfileValidationError(
            "Voice profile IDs or ordering do not match the expected "
            "companion catalog: "
            f"{actual_ids!r}."
        )

    unique_fields = (*VOICE_DIMENSION_FIELDS, "voice_description")
    for field in unique_fields:
        values = {
            _normalise_wording(getattr(profile, field)) for profile in profiles
        }
        if len(values) != len(EXPECTED_COMPANION_IDS):
            raise VoiceProfileValidationError(
                f"Every profile must have a unique {field.replace('_', ' ')}."
            )

    return tuple(profiles)


def load_voice_profiles(path: Path = CONFIG_PATH) -> tuple[VoiceProfile, ...]:
    """Load and validate voice profiles without importing model dependencies."""

    try:
        with path.open(encoding="utf-8") as profile_file:
            raw_profiles = json.load(profile_file)
    except (OSError, json.JSONDecodeError) as error:
        raise VoiceProfileValidationError(
            f"Could not load voice profiles from {path}: {error}"
        ) from error
    return validate_voice_profiles(raw_profiles)


VOICE_PROFILES = load_voice_profiles()
VOICE_PROFILES_BY_ID: Mapping[str, VoiceProfile] = MappingProxyType(
    {profile.companion_id: profile for profile in VOICE_PROFILES}
)


def get_voice_profile(companion_id: str) -> VoiceProfile:
    """Return one validated profile, raising a clear error for an unknown ID."""

    try:
        return VOICE_PROFILES_BY_ID[companion_id]
    except KeyError as error:
        available = ", ".join(EXPECTED_COMPANION_IDS)
        raise ValueError(
            f"Unknown companion {companion_id!r}. Available IDs: {available}."
        ) from error
