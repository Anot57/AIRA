"""Standard-library checks for Local Voice Lab milestone 1."""

from __future__ import annotations

import builtins
import copy
import importlib
import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SERVICE_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = SERVICE_ROOT.parents[1]
SOURCE_ROOT = SERVICE_ROOT / "src"
sys.path.insert(0, str(SOURCE_ROOT))

from local_voice_api.generation import (  # noqa: E402
    DEFAULT_OUTPUT_DIR,
    is_e_drive_path,
    select_attention_implementation,
)
from local_voice_api.profiles import (  # noqa: E402
    CONFIG_PATH,
    EXPECTED_COMPANION_IDS,
    FORBIDDEN_VOICE_WORDING,
    PROFILE_FIELDS,
    VOICE_DIMENSION_FIELDS,
    VOICE_PROFILES,
    VoiceProfileValidationError,
    validate_voice_profiles,
)


def _load_raw_profiles() -> list[dict[str, object]]:
    with CONFIG_PATH.open(encoding="utf-8") as profile_file:
        return json.load(profile_file)


class VoiceProfileTests(unittest.TestCase):
    def test_exactly_twenty_profiles(self) -> None:
        self.assertEqual(20, len(VOICE_PROFILES))
        self.assertEqual(20, len(EXPECTED_COMPANION_IDS))

    def test_ids_exactly_match_companion_catalog(self) -> None:
        catalog_path = (
            REPOSITORY_ROOT
            / "apps"
            / "mobile"
            / "assets"
            / "data"
            / "companions.json"
        )
        with catalog_path.open(encoding="utf-8") as catalog_file:
            catalog = json.load(catalog_file)

        catalog_ids = tuple(companion["id"] for companion in catalog)
        profile_ids = tuple(profile.companion_id for profile in VOICE_PROFILES)
        self.assertEqual(EXPECTED_COMPANION_IDS, catalog_ids)
        self.assertEqual(catalog_ids, profile_ids)

    def test_every_profile_is_adult_and_ai_generated(self) -> None:
        for profile in VOICE_PROFILES:
            with self.subTest(companion_id=profile.companion_id):
                self.assertIs(profile.adult, True)
                self.assertIs(profile.ai_generated, True)
                self.assertIn("adult", profile.voice_description.casefold())
                self.assertIn("ai-generated", profile.voice_description.casefold())

    def test_voice_descriptions_are_unique(self) -> None:
        descriptions = {
            profile.voice_description.casefold() for profile in VOICE_PROFILES
        }
        self.assertEqual(20, len(descriptions))

    def test_voice_dimensions_are_meaningfully_distinct(self) -> None:
        for field in VOICE_DIMENSION_FIELDS:
            with self.subTest(field=field):
                values = {
                    getattr(profile, field).casefold() for profile in VOICE_PROFILES
                }
                self.assertEqual(20, len(values))

    def test_configuration_has_no_imitation_field_or_wording(self) -> None:
        raw_profiles = _load_raw_profiles()
        forbidden_fields = {
            "celebrity",
            "real_person",
            "real_person_reference",
            "imitation",
            "imitates",
            "impersonates",
        }
        for raw_profile in raw_profiles:
            with self.subTest(companion_id=raw_profile["companion_id"]):
                self.assertEqual(PROFILE_FIELDS, set(raw_profile))
                self.assertTrue(forbidden_fields.isdisjoint(raw_profile))
                wording = " ".join(
                    value for value in raw_profile.values() if isinstance(value, str)
                ).casefold().replace("-", " ")
                for forbidden_wording in FORBIDDEN_VOICE_WORDING:
                    self.assertNotIn(
                        forbidden_wording.casefold().replace("-", " "), wording
                    )

    def test_validator_rejects_imitation_wording(self) -> None:
        for forbidden_wording in (
            "celebrity",
            "real person",
            "sounds like",
            "modelled after",
            "based on",
            "replica of",
            "resembling",
        ):
            with self.subTest(wording=forbidden_wording):
                raw_profiles = copy.deepcopy(_load_raw_profiles())
                raw_profiles[0]["voice_description"] += f" {forbidden_wording}."
                with self.assertRaises(VoiceProfileValidationError):
                    validate_voice_profiles(raw_profiles)

    def test_validator_rejects_imitation_fields(self) -> None:
        raw_profiles = _load_raw_profiles()
        raw_profiles[0]["real_person_reference"] = "not allowed"
        with self.assertRaises(VoiceProfileValidationError):
            validate_voice_profiles(raw_profiles)

    def test_default_output_location_is_on_wsl_e_drive(self) -> None:
        self.assertEqual(
            "/mnt/e/aira-local-runtime/generated/voices",
            DEFAULT_OUTPUT_DIR.as_posix(),
        )
        self.assertTrue(is_e_drive_path(DEFAULT_OUTPUT_DIR))
        self.assertTrue(is_e_drive_path("E:/aira-local-runtime/generated/voices"))
        self.assertFalse(is_e_drive_path("E:relative-output"))
        self.assertFalse(is_e_drive_path("/tmp/generated/voices"))

    def test_cli_defaults_to_aanya(self) -> None:
        cli = importlib.import_module("local_voice_api.cli")
        arguments = cli.build_parser().parse_args([])
        self.assertEqual("aanya", arguments.companion)
        self.assertEqual(DEFAULT_OUTPUT_DIR, arguments.output_dir)

    def test_attention_selection_prefers_sdpa_with_eager_fallback(self) -> None:
        sdpa_torch = SimpleNamespace(
            nn=SimpleNamespace(
                functional=SimpleNamespace(
                    scaled_dot_product_attention=lambda *args: None
                )
            )
        )
        eager_torch = SimpleNamespace(
            nn=SimpleNamespace(functional=SimpleNamespace())
        )
        self.assertEqual("sdpa", select_attention_implementation(sdpa_torch))
        self.assertEqual("eager", select_attention_implementation(eager_torch))

    def test_list_path_does_not_import_model_runtime(self) -> None:
        real_import = builtins.__import__

        def guarded_import(name: str, *args: object, **kwargs: object) -> object:
            if name.split(".", maxsplit=1)[0] in {"qwen_tts", "torch"}:
                self.fail(f"--list attempted to import {name}")
            return real_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=guarded_import):
            cli = importlib.import_module("local_voice_api.cli")
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = cli.main(["--list"])

        self.assertEqual(0, exit_code)
        self.assertEqual(20, len(output.getvalue().splitlines()))
        self.assertNotIn("qwen_tts", sys.modules)
        self.assertNotIn("torch", sys.modules)


if __name__ == "__main__":
    unittest.main()
