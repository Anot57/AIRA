"""Standard-library checks for approved reusable companion synthesis."""

from __future__ import annotations

import builtins
import copy
import importlib
import io
import json
import sys
import tempfile
import unittest
import wave
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SERVICE_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = SERVICE_ROOT / "src"
sys.path.insert(0, str(SOURCE_ROOT))

import local_voice_api.synthesis as synthesis_module  # noqa: E402
from local_voice_api.approved_references import (  # noqa: E402
    APPROVED_VOICE_REFERENCES,
    MANIFEST_FIELDS,
    MANIFEST_PATH,
    VOICE_DESIGN_MODEL_ID,
    ApprovedReferenceValidationError,
    load_validated_voice_reference,
    resolve_approved_voice_reference,
    validate_approved_voice_references,
    validate_reference_metadata,
)
from local_voice_api.generation import is_e_drive_path  # noqa: E402
from local_voice_api.synthesis import (  # noqa: E402
    BASE_MODEL_ID,
    DEFAULT_REFERENCE_DIR,
    DEFAULT_SYNTHESIS_OUTPUT_DIR,
    DEFAULT_SYNTHESIS_SEED,
    MAX_SYNTHESIS_TEXT_CHARACTERS,
    select_base_runtime,
    validate_synthesis_text,
)


def _load_manifest_data() -> list[dict[str, object]]:
    with MANIFEST_PATH.open(encoding="utf-8") as manifest_file:
        return json.load(manifest_file)


def _write_pcm_wav(path: Path, sample_rate: int = 24_000) -> None:
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(b"\x00\x00" * 240)


class ReferenceFixtureTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(dir=SERVICE_ROOT)
        self.addCleanup(self.temporary_directory.cleanup)
        self.reference_dir = Path(self.temporary_directory.name)
        self.approved = APPROVED_VOICE_REFERENCES[0]
        self.wav_path = self.reference_dir / self.approved.wav_filename
        self.metadata_path = (
            self.reference_dir / self.approved.metadata_filename
        )
        _write_pcm_wav(self.wav_path)
        self.metadata: dict[str, object] = {
            "adult": True,
            "ai_generated": True,
            "companion_id": self.approved.companion_id,
            "created_at_utc": "2026-08-31T00:00:00Z",
            "model_id": VOICE_DESIGN_MODEL_ID,
            "sample_rate": 24_000,
            "seed": self.approved.seed,
            "transcript": "I'm Aanya, an AI voice speaking in a local test.",
            "voice_description": "Test fixture only.",
        }
        self._write_metadata(self.metadata)

    def _write_metadata(self, metadata: object) -> None:
        self.metadata_path.write_text(
            json.dumps(metadata), encoding="utf-8"
        )


class ApprovedManifestTests(unittest.TestCase):
    def test_manifest_initially_approves_only_aanya(self) -> None:
        self.assertEqual(1, len(APPROVED_VOICE_REFERENCES))
        approved = APPROVED_VOICE_REFERENCES[0]
        self.assertEqual("aanya", approved.companion_id)
        self.assertEqual(20260831, approved.seed)
        self.assertEqual("aanya_seed_20260831.wav", approved.wav_filename)
        self.assertEqual(
            "aanya_seed_20260831.json", approved.metadata_filename
        )
        self.assertFalse(Path(approved.wav_filename).is_absolute())
        self.assertFalse(Path(approved.metadata_filename).is_absolute())

    def test_manifest_has_exact_schema(self) -> None:
        for raw_reference in _load_manifest_data():
            self.assertEqual(MANIFEST_FIELDS, set(raw_reference))

    def test_manifest_rejects_invalid_records(self) -> None:
        cases: dict[str, object] = {}

        missing_field = copy.deepcopy(_load_manifest_data())
        del missing_field[0]["wav_filename"]
        cases["missing field"] = missing_field

        extra_field = copy.deepcopy(_load_manifest_data())
        extra_field[0]["absolute_path"] = "E:/voices/aanya.wav"
        cases["unexpected field"] = extra_field

        bool_seed = copy.deepcopy(_load_manifest_data())
        bool_seed[0]["seed"] = True
        cases["bool seed"] = bool_seed

        negative_seed = copy.deepcopy(_load_manifest_data())
        negative_seed[0]["seed"] = -1
        cases["negative seed"] = negative_seed

        absolute_wav = copy.deepcopy(_load_manifest_data())
        absolute_wav[0]["wav_filename"] = "/mnt/e/aanya.wav"
        cases["absolute WAV"] = absolute_wav

        windows_wav = copy.deepcopy(_load_manifest_data())
        windows_wav[0]["wav_filename"] = "E:\\voices\\aanya.wav"
        cases["Windows WAV"] = windows_wav

        traversal = copy.deepcopy(_load_manifest_data())
        traversal[0]["metadata_filename"] = "../aanya.json"
        cases["traversal"] = traversal

        unknown_companion = copy.deepcopy(_load_manifest_data())
        unknown_companion[0].update(
            {
                "companion_id": "unknown",
                "wav_filename": "unknown_seed_20260831.wav",
                "metadata_filename": "unknown_seed_20260831.json",
            }
        )
        cases["unknown companion"] = unknown_companion

        duplicate = copy.deepcopy(_load_manifest_data())
        duplicate.append(copy.deepcopy(duplicate[0]))
        cases["duplicate companion"] = duplicate

        for label, raw_manifest in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(ApprovedReferenceValidationError):
                    validate_approved_voice_references(raw_manifest)


class ApprovedReferenceTests(ReferenceFixtureTestCase):
    def test_resolution_uses_only_exact_approved_filenames(self) -> None:
        newer_decoy = self.reference_dir / "aanya_seed_20991231.wav"
        _write_pcm_wav(newer_decoy)

        resolved = resolve_approved_voice_reference(
            "aanya", self.reference_dir
        )

        self.assertEqual(self.wav_path.resolve(), resolved.wav_path)
        self.assertEqual(self.metadata_path.resolve(), resolved.metadata_path)
        self.assertNotEqual(newer_decoy.resolve(), resolved.wav_path)

    def test_valid_reference_metadata_and_wav_are_accepted(self) -> None:
        validated = load_validated_voice_reference(
            "aanya", self.reference_dir
        )
        self.assertEqual(self.approved, validated.approved)
        self.assertEqual(self.metadata["transcript"], validated.transcript)
        self.assertEqual(24_000, validated.sample_rate)

    def test_metadata_validation_rejects_provenance_and_safety_errors(self) -> None:
        resolved = resolve_approved_voice_reference(
            "aanya", self.reference_dir
        )
        invalid_values = {
            "companion ID": ("companion_id", "tara"),
            "seed": ("seed", 1),
            "bool seed": ("seed", True),
            "adult flag": ("adult", False),
            "AI flag": ("ai_generated", False),
            "empty transcript": ("transcript", ""),
            "blank transcript": ("transcript", "   "),
            "wrong model": ("model_id", BASE_MODEL_ID),
            "zero sample rate": ("sample_rate", 0),
            "string sample rate": ("sample_rate", "24000"),
            "bool sample rate": ("sample_rate", True),
            "wrong optional WAV name": ("wav_filename", "other.wav"),
        }

        for label, (field, value) in invalid_values.items():
            with self.subTest(case=label):
                metadata = copy.deepcopy(self.metadata)
                metadata[field] = value
                with self.assertRaises(ApprovedReferenceValidationError):
                    validate_reference_metadata(resolved, metadata)

    def test_metadata_sample_rate_must_match_wav(self) -> None:
        resolved = resolve_approved_voice_reference(
            "aanya", self.reference_dir
        )
        metadata = copy.deepcopy(self.metadata)
        metadata["sample_rate"] = 48_000
        with self.assertRaises(ApprovedReferenceValidationError):
            validate_reference_metadata(resolved, metadata)

    def test_missing_wav_is_rejected(self) -> None:
        self.wav_path.unlink()
        with self.assertRaises(ApprovedReferenceValidationError):
            resolve_approved_voice_reference("aanya", self.reference_dir)

    def test_malformed_metadata_is_rejected(self) -> None:
        self.metadata_path.write_text("{", encoding="utf-8")
        with self.assertRaises(ApprovedReferenceValidationError):
            load_validated_voice_reference("aanya", self.reference_dir)

    def test_unapproved_companion_is_rejected(self) -> None:
        with self.assertRaises(ApprovedReferenceValidationError):
            resolve_approved_voice_reference("tara", self.reference_dir)


class SynthesisConfigurationTests(unittest.TestCase):
    def test_text_limits(self) -> None:
        for invalid_text in (None, 7, "", "   "):
            with self.subTest(text=invalid_text):
                with self.assertRaises(ValueError):
                    validate_synthesis_text(invalid_text)

        maximum_text = "x" * MAX_SYNTHESIS_TEXT_CHARACTERS
        self.assertEqual(maximum_text, validate_synthesis_text(maximum_text))
        with self.assertRaises(ValueError):
            validate_synthesis_text(maximum_text + "x")

    def test_default_paths_and_seed(self) -> None:
        self.assertEqual(
            "/mnt/e/aira-local-runtime/generated/voices",
            DEFAULT_REFERENCE_DIR.as_posix(),
        )
        self.assertEqual(
            "/mnt/e/aira-local-runtime/generated/runtime",
            DEFAULT_SYNTHESIS_OUTPUT_DIR.as_posix(),
        )
        self.assertTrue(is_e_drive_path(DEFAULT_REFERENCE_DIR))
        self.assertTrue(is_e_drive_path(DEFAULT_SYNTHESIS_OUTPUT_DIR))
        self.assertEqual(20260901, DEFAULT_SYNTHESIS_SEED)

    def test_turing_selects_float32_and_eager_without_runtime_imports(self) -> None:
        fake_cuda = SimpleNamespace(
            get_device_capability=mock.Mock(return_value=(7, 5)),
            get_device_name=mock.Mock(return_value="NVIDIA RTX 2070 Max-Q"),
        )
        fake_torch = SimpleNamespace(
            cuda=fake_cuda,
            float16="float16 sentinel",
            float32="float32 sentinel",
        )
        real_import = builtins.__import__

        def guarded_import(name: str, *args: object, **kwargs: object) -> object:
            if name.split(".", maxsplit=1)[0] in {
                "qwen_tts",
                "soundfile",
                "torch",
            }:
                self.fail(f"Base runtime selection attempted to import {name}")
            return real_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=guarded_import):
            runtime = select_base_runtime(fake_torch)

        self.assertEqual("NVIDIA RTX 2070 Max-Q", runtime.gpu_name)
        self.assertEqual((7, 5), runtime.compute_capability)
        self.assertEqual("7.5", runtime.compute_capability_label)
        self.assertEqual("float32 sentinel", runtime.dtype)
        self.assertEqual("float32", runtime.dtype_name)
        self.assertEqual("eager", runtime.attention_backend)
        self.assertFalse(hasattr(fake_torch, "bfloat16"))
        fake_cuda.get_device_name.assert_called_once_with(0)
        fake_cuda.get_device_capability.assert_called_once_with(0)

    def test_ampere_selects_float16_and_sdpa_without_runtime_imports(self) -> None:
        fake_cuda = SimpleNamespace(
            get_device_capability=mock.Mock(return_value=(8, 6)),
            get_device_name=mock.Mock(return_value="NVIDIA RTX 3080"),
        )
        fake_torch = SimpleNamespace(
            cuda=fake_cuda,
            float16="float16 sentinel",
            float32="float32 sentinel",
        )
        real_import = builtins.__import__

        def guarded_import(name: str, *args: object, **kwargs: object) -> object:
            if name.split(".", maxsplit=1)[0] in {
                "qwen_tts",
                "soundfile",
                "torch",
            }:
                self.fail(f"Base runtime selection attempted to import {name}")
            return real_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=guarded_import):
            runtime = select_base_runtime(fake_torch)

        self.assertEqual("NVIDIA RTX 3080", runtime.gpu_name)
        self.assertEqual((8, 6), runtime.compute_capability)
        self.assertEqual("8.6", runtime.compute_capability_label)
        self.assertEqual("float16 sentinel", runtime.dtype)
        self.assertEqual("float16", runtime.dtype_name)
        self.assertEqual("sdpa", runtime.attention_backend)
        fake_cuda.get_device_name.assert_called_once_with(0)
        fake_cuda.get_device_capability.assert_called_once_with(0)

    def test_list_approved_and_cli_defaults_do_not_import_runtime(self) -> None:
        real_import = builtins.__import__

        def guarded_import(name: str, *args: object, **kwargs: object) -> object:
            if name.split(".", maxsplit=1)[0] in {
                "qwen_tts",
                "soundfile",
                "torch",
            }:
                self.fail(f"approved listing attempted to import {name}")
            return real_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=guarded_import):
            cli = importlib.import_module("local_voice_api.synthesis_cli")
            arguments = cli.build_parser().parse_args([])
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = cli.main(["--list-approved"])

        self.assertEqual("aanya", arguments.companion)
        self.assertIsNone(arguments.text)
        self.assertEqual(DEFAULT_REFERENCE_DIR, arguments.reference_dir)
        self.assertEqual(DEFAULT_SYNTHESIS_OUTPUT_DIR, arguments.output_dir)
        self.assertEqual(DEFAULT_SYNTHESIS_SEED, arguments.seed)
        self.assertEqual(0, exit_code)
        self.assertIn("aanya: seed=20260831", output.getvalue())
        self.assertNotIn("qwen_tts", sys.modules)
        self.assertNotIn("soundfile", sys.modules)
        self.assertNotIn("torch", sys.modules)


class ReusableRuntimeTests(ReferenceFixtureTestCase):
    def test_model_and_prompt_are_reused_with_official_call_shapes(self) -> None:
        fake_model = SimpleNamespace(
            create_voice_clone_prompt=mock.Mock(return_value=["cached prompt"]),
            generate_voice_clone=mock.Mock(
                side_effect=[([0.1, 0.2], 24_000), ([0.3, 0.4], 24_000)]
            ),
        )
        fake_model_class = SimpleNamespace(
            from_pretrained=mock.Mock(return_value=fake_model)
        )
        fake_cuda = SimpleNamespace(
            OutOfMemoryError=RuntimeError,
            empty_cache=mock.Mock(),
            get_device_capability=mock.Mock(return_value=(8, 0)),
            get_device_name=mock.Mock(return_value="Fake Ampere GPU"),
            is_available=mock.Mock(return_value=True),
            manual_seed_all=mock.Mock(),
        )
        fake_torch = SimpleNamespace(
            cuda=fake_cuda,
            float16="float16",
            float32="float32",
            manual_seed=mock.Mock(),
            nn=SimpleNamespace(
                functional=SimpleNamespace(
                    scaled_dot_product_attention=lambda *args: None
                )
            ),
        )
        fake_numpy = SimpleNamespace(
            random=SimpleNamespace(seed=mock.Mock())
        )

        class FakeSoundFile:
            @staticmethod
            def write(path: str, *args: object, **kwargs: object) -> None:
                Path(path).write_bytes(b"fake local audio")

        def reset_runtime_state() -> None:
            synthesis_module._prompt_cache.clear()
            synthesis_module._base_model = None
            synthesis_module._base_runtime = None
            synthesis_module._numpy_module = None
            synthesis_module._soundfile_module = None
            synthesis_module._torch_module = None
            synthesis_module._runtime_poisoned = False
            synthesis_module._runtime_shutdown = False

        reset_runtime_state()
        self.addCleanup(reset_runtime_state)

        text_one = "First local synthesis line."
        text_two = "Second local synthesis line."
        with mock.patch.object(
            synthesis_module, "validate_e_drive_runtime", return_value=None
        ), mock.patch.object(
            synthesis_module, "_load_torch", return_value=fake_torch
        ), mock.patch.object(
            synthesis_module,
            "_load_model_dependencies",
            return_value=(fake_numpy, FakeSoundFile, fake_model_class),
        ), self.assertLogs(
            "local_voice_api.synthesis", level="INFO"
        ) as runtime_logs:
            first = synthesis_module.synthesize_companion_voice(
                "aanya",
                text_one,
                reference_dir=self.reference_dir,
                output_dir=self.reference_dir,
            )
            second = synthesis_module.synthesize_companion_voice(
                "aanya",
                text_two,
                reference_dir=self.reference_dir,
                output_dir=self.reference_dir,
            )
            synthesis_module.shutdown_voice_clone_runtime()

        fake_model_class.from_pretrained.assert_called_once_with(
            BASE_MODEL_ID,
            device_map="cuda:0",
            dtype="float16",
            attn_implementation="sdpa",
        )
        self.assertEqual(1, len(runtime_logs.output))
        self.assertIn("GPU=Fake Ampere GPU", runtime_logs.output[0])
        self.assertIn("compute capability=8.0", runtime_logs.output[0])
        self.assertIn("dtype=torch.float16", runtime_logs.output[0])
        self.assertIn("attention backend=sdpa", runtime_logs.output[0])
        fake_model.create_voice_clone_prompt.assert_called_once_with(
            ref_audio=str(self.wav_path.resolve()),
            ref_text=self.metadata["transcript"],
            x_vector_only_mode=False,
        )
        self.assertEqual(2, fake_model.generate_voice_clone.call_count)
        first_call = fake_model.generate_voice_clone.call_args_list[0]
        second_call = fake_model.generate_voice_clone.call_args_list[1]
        self.assertEqual(
            {
                "text": text_one,
                "language": "English",
                "voice_clone_prompt": ["cached prompt"],
            },
            first_call.kwargs,
        )
        self.assertEqual(
            {
                "text": text_two,
                "language": "English",
                "voice_clone_prompt": ["cached prompt"],
            },
            second_call.kwargs,
        )
        self.assertTrue(first.wav_path.is_file())
        self.assertTrue(second.metadata_path.is_file())
        output_metadata = json.loads(
            second.metadata_path.read_text(encoding="utf-8")
        )
        self.assertEqual(BASE_MODEL_ID, output_metadata["model_id"])
        self.assertEqual(text_two, output_metadata["synthesized_text"])
        self.assertEqual(self.approved.seed, output_metadata["reference_seed"])
        self.assertEqual(
            self.approved.wav_filename,
            output_metadata["reference_wav_filename"],
        )
        self.assertEqual(DEFAULT_SYNTHESIS_SEED, output_metadata["output_seed"])
        self.assertEqual("sdpa", output_metadata["attention_backend"])
        self.assertEqual("8.0", output_metadata["cuda_compute_capability"])
        self.assertEqual("float16", output_metadata["runtime_dtype"])
        self.assertIs(output_metadata["adult"], True)
        self.assertIs(output_metadata["ai_generated"], True)
        self.assertEqual({}, synthesis_module._prompt_cache)
        self.assertIsNone(synthesis_module._base_model)
        self.assertIs(synthesis_module._runtime_shutdown, True)
        fake_cuda.empty_cache.assert_called_once_with()

    def test_device_assertion_blocks_in_process_model_reload(self) -> None:
        fake_model_class = SimpleNamespace(
            from_pretrained=mock.Mock(
                side_effect=RuntimeError(
                    "probability tensor contains either inf, nan or element < 0; "
                    "CUDA error: device-side assert triggered"
                )
            )
        )
        fake_cuda = SimpleNamespace(
            OutOfMemoryError=RuntimeError,
            empty_cache=mock.Mock(),
            get_device_capability=mock.Mock(return_value=(7, 5)),
            get_device_name=mock.Mock(return_value="Fake Turing GPU"),
            is_available=mock.Mock(return_value=True),
        )
        fake_torch = SimpleNamespace(
            cuda=fake_cuda,
            float16="float16",
            float32="float32",
        )

        def reset_runtime_state() -> None:
            synthesis_module._prompt_cache.clear()
            synthesis_module._base_model = None
            synthesis_module._base_runtime = None
            synthesis_module._numpy_module = None
            synthesis_module._soundfile_module = None
            synthesis_module._torch_module = None
            synthesis_module._runtime_poisoned = False
            synthesis_module._runtime_shutdown = False

        reset_runtime_state()
        self.addCleanup(reset_runtime_state)

        with mock.patch.object(
            synthesis_module, "_load_torch", return_value=fake_torch
        ) as load_torch, mock.patch.object(
            synthesis_module,
            "_load_model_dependencies",
            return_value=(object(), object(), fake_model_class),
        ) as load_dependencies:
            with self.assertRaisesRegex(
                synthesis_module.VoiceSynthesisError,
                "poisoned.*cannot safely retry in-process",
            ):
                synthesis_module._ensure_base_model()
            with self.assertRaisesRegex(
                synthesis_module.VoiceSynthesisError,
                "start a fresh process",
            ):
                synthesis_module._ensure_base_model()
            synthesis_module.shutdown_voice_clone_runtime()

        load_torch.assert_called_once_with()
        load_dependencies.assert_called_once_with()
        fake_model_class.from_pretrained.assert_called_once_with(
            BASE_MODEL_ID,
            device_map="cuda:0",
            dtype="float32",
            attn_implementation="eager",
        )
        fake_cuda.empty_cache.assert_not_called()
        self.assertTrue(synthesis_module._runtime_poisoned)


if __name__ == "__main__":
    unittest.main()
