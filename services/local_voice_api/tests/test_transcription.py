"""Standard-library checks for private local Faster-Whisper transcription."""

from __future__ import annotations

import builtins
import hashlib
import importlib
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SERVICE_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = SERVICE_ROOT / "src"
sys.path.insert(0, str(SOURCE_ROOT))

import local_voice_api.transcription as transcription_module  # noqa: E402
from local_voice_api.transcription import (  # noqa: E402
    BEAM_SIZE,
    COMPUTE_TYPE,
    CONDITION_ON_PREVIOUS_TEXT,
    DEFAULT_CPU_THREADS,
    DEFAULT_MODEL_DIR,
    DEFAULT_TRANSCRIPT_OUTPUT_DIR,
    DEVICE,
    LANGUAGE,
    MAX_INPUT_BYTES,
    MODEL_NAME,
    NUM_WORKERS,
    SUPPORTED_AUDIO_EXTENSIONS,
    VAD_FILTER,
    TranscriptionError,
    describe_transcription_configuration,
    is_e_drive_path,
    validate_audio_input,
    validate_storage_directory,
)

FORBIDDEN_RUNTIME_IMPORTS = {
    "faster_whisper",
    "qwen_tts",
    "soundfile",
    "torch",
}
_REAL_IMPORT = builtins.__import__
_IMPORT_PATCHER = None


def setUpModule() -> None:
    global _IMPORT_PATCHER

    imported = FORBIDDEN_RUNTIME_IMPORTS.intersection(sys.modules)
    if imported:
        raise AssertionError(f"Runtime dependencies were eagerly imported: {imported}")

    def guarded_import(name: str, *args: object, **kwargs: object) -> object:
        if name.split(".", maxsplit=1)[0] in FORBIDDEN_RUNTIME_IMPORTS:
            raise AssertionError(f"Lightweight STT test attempted to import {name}")
        return _REAL_IMPORT(name, *args, **kwargs)

    _IMPORT_PATCHER = mock.patch("builtins.__import__", side_effect=guarded_import)
    _IMPORT_PATCHER.start()


def tearDownModule() -> None:
    if _IMPORT_PATCHER is not None:
        _IMPORT_PATCHER.stop()
    imported = FORBIDDEN_RUNTIME_IMPORTS.intersection(sys.modules)
    if imported:
        raise AssertionError(f"Runtime dependencies were imported by tests: {imported}")


class TranscriptionTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(dir=SERVICE_ROOT)
        self.addCleanup(self.temporary_directory.cleanup)
        self.runtime_root = Path(self.temporary_directory.name).resolve()
        self.audio_path = self.runtime_root / "short_turn.wav"
        self.audio_bytes = b"fake local audio bytes"
        self.audio_path.write_bytes(self.audio_bytes)
        self.output_dir = self.runtime_root / "transcripts"
        self.model_dir = self.runtime_root / "models" / "faster-whisper"
        self._reset_runtime()
        self.addCleanup(self._reset_runtime)

    @staticmethod
    def _reset_runtime() -> None:
        transcription_module._whisper_model = None
        transcription_module._loaded_runtime_config = None
        transcription_module._runtime_shutdown = False

    @staticmethod
    def _info() -> SimpleNamespace:
        return SimpleNamespace(
            language="en",
            language_probability=0.975,
            duration=2.75,
        )

    @staticmethod
    def _segments(exhausted: list[bool]):
        yield SimpleNamespace(start=0.1, end=1.0, text="  Hello")
        yield SimpleNamespace(start=1.1, end=2.4, text="world.  ")
        exhausted.append(True)


class TranscriptionConfigurationTests(TranscriptionTestCase):
    def test_cpu_int8_defaults_are_dependency_free(self) -> None:
        configuration = describe_transcription_configuration()

        self.assertEqual("base.en", MODEL_NAME)
        self.assertEqual("cpu", DEVICE)
        self.assertEqual("int8", COMPUTE_TYPE)
        self.assertEqual(6, DEFAULT_CPU_THREADS)
        self.assertEqual(1, NUM_WORKERS)
        self.assertEqual("en", LANGUAGE)
        self.assertEqual(1, BEAM_SIZE)
        self.assertIs(VAD_FILTER, True)
        self.assertIs(CONDITION_ON_PREVIOUS_TEXT, False)
        self.assertEqual(
            frozenset({".wav", ".mp3", ".m4a", ".ogg", ".webm"}),
            SUPPORTED_AUDIO_EXTENSIONS,
        )
        self.assertEqual(
            "/mnt/e/aira-local-runtime/models/faster-whisper",
            DEFAULT_MODEL_DIR.as_posix(),
        )
        self.assertEqual(
            "/mnt/e/aira-local-runtime/generated/transcripts",
            DEFAULT_TRANSCRIPT_OUTPUT_DIR.as_posix(),
        )
        self.assertEqual("base.en", configuration["model_name"])
        self.assertEqual("cpu", configuration["device"])
        self.assertEqual("int8", configuration["compute_type"])
        self.assertEqual(6, configuration["cpu_threads"])
        self.assertEqual(1, configuration["workers"])
        self.assertEqual(1, configuration["beam_size"])
        self.assertIs(configuration["vad_filter"], True)
        self.assertIs(configuration["condition_on_previous_text"], False)

    def test_cli_show_config_does_not_import_optional_runtimes(self) -> None:
        load_model = mock.Mock(side_effect=AssertionError("model import attempted"))
        with mock.patch.object(
            transcription_module, "_load_whisper_model_class", load_model
        ):
            cli = importlib.import_module("local_voice_api.transcription_cli")
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = cli.main(["--show-config"])

        configuration = json.loads(output.getvalue())
        self.assertEqual(0, exit_code)
        self.assertEqual("base.en", configuration["model_name"])
        self.assertEqual("cpu", configuration["device"])
        self.assertEqual("int8", configuration["compute_type"])
        self.assertEqual(6, configuration["cpu_threads"])
        load_model.assert_not_called()
        self.assertFalse(FORBIDDEN_RUNTIME_IMPORTS.intersection(sys.modules))


class TranscriptionValidationTests(TranscriptionTestCase):
    def test_invalid_inputs_fail_before_model_import(self) -> None:
        missing = self.runtime_root / "missing.wav"
        directory = self.runtime_root / "directory.wav"
        directory.mkdir()
        empty = self.runtime_root / "empty.wav"
        empty.touch()
        unsupported = self.runtime_root / "turn.txt"
        unsupported.write_bytes(b"not audio")
        cases = {
            "missing": (missing, "does not exist"),
            "directory": (directory, "not a regular file"),
            "empty": (empty, "is empty"),
            "unsupported": (unsupported, "Unsupported audio extension"),
        }

        with mock.patch.object(
            transcription_module, "_load_whisper_model_class"
        ) as load_model:
            for label, (path, message) in cases.items():
                with self.subTest(case=label):
                    with self.assertRaisesRegex(TranscriptionError, message):
                        transcription_module.transcribe_audio(
                            path,
                            output_dir=self.output_dir,
                            model_dir=self.model_dir,
                        )

        load_model.assert_not_called()

    def test_extensions_and_fifty_mib_size_boundary(self) -> None:
        for extension in SUPPORTED_AUDIO_EXTENSIONS:
            with self.subTest(extension=extension):
                audio_path = self.runtime_root / f"turn{extension.upper()}"
                audio_path.write_bytes(b"audio")
                self.assertEqual(audio_path.resolve(), validate_audio_input(audio_path))

        boundary_path = self.runtime_root / "boundary.wav"
        with boundary_path.open("wb") as boundary_file:
            boundary_file.truncate(MAX_INPUT_BYTES)
        self.assertEqual(boundary_path.resolve(), validate_audio_input(boundary_path))

        with boundary_path.open("r+b") as boundary_file:
            boundary_file.truncate(MAX_INPUT_BYTES + 1)
        with self.assertRaisesRegex(TranscriptionError, "50 MiB limit"):
            validate_audio_input(boundary_path)

    def test_e_drive_and_resolved_symlink_escape_are_rejected(self) -> None:
        self.assertTrue(is_e_drive_path(self.audio_path))
        self.assertTrue(is_e_drive_path("/mnt/e/turn.wav"))
        self.assertFalse(is_e_drive_path("C:/turn.wav"))
        self.assertFalse(is_e_drive_path("/tmp/turn.wav"))

        with self.assertRaisesRegex(TranscriptionError, "under /mnt/e"):
            validate_audio_input("C:/turn.wav")
        with self.assertRaisesRegex(TranscriptionError, "under /mnt/e"):
            validate_storage_directory("C:/models", "Model directory")
        with self.assertRaisesRegex(TranscriptionError, "under /mnt/e"):
            validate_storage_directory("/tmp/transcripts", "Output directory")

        with mock.patch.object(
            Path, "resolve", return_value=Path("C:/escaped/turn.wav")
        ):
            with self.assertRaisesRegex(TranscriptionError, "resolves outside /mnt/e"):
                validate_audio_input(self.audio_path)
            with self.assertRaisesRegex(TranscriptionError, "resolves outside /mnt/e"):
                validate_storage_directory(self.model_dir, "Model directory")


class ReusableTranscriptionRuntimeTests(TranscriptionTestCase):
    def test_model_reuse_consumes_segments_and_writes_metadata(self) -> None:
        exhausted_first: list[bool] = []
        exhausted_second: list[bool] = []
        fake_model = SimpleNamespace(
            transcribe=mock.Mock(
                side_effect=[
                    (self._segments(exhausted_first), self._info()),
                    (self._segments(exhausted_second), self._info()),
                ]
            )
        )
        fake_model_class = mock.Mock(return_value=fake_model)
        second_audio = self.runtime_root / "second_turn.ogg"
        second_audio_bytes = b"second fake local audio"
        second_audio.write_bytes(second_audio_bytes)

        first = transcription_module.transcribe_audio(
            self.audio_path,
            output_dir=self.output_dir,
            model_dir=self.model_dir,
            whisper_model_class=fake_model_class,
        )
        second = transcription_module.transcribe_audio(
            second_audio,
            output_dir=self.output_dir,
            model_dir=self.model_dir,
            whisper_model_class=fake_model_class,
        )

        fake_model_class.assert_called_once_with(
            "base.en",
            device="cpu",
            compute_type="int8",
            cpu_threads=6,
            num_workers=1,
            download_root=str(self.model_dir.resolve()),
        )
        self.assertEqual(2, fake_model.transcribe.call_count)
        self.assertEqual(
            {
                "language": "en",
                "beam_size": 1,
                "vad_filter": True,
                "condition_on_previous_text": False,
            },
            fake_model.transcribe.call_args_list[0].kwargs,
        )
        self.assertEqual(
            str(self.audio_path.resolve()),
            fake_model.transcribe.call_args_list[0].args[0],
        )
        self.assertEqual([True], exhausted_first)
        self.assertEqual([True], exhausted_second)
        self.assertEqual("Hello world.", first.transcript)
        self.assertEqual(0.1, first.segments[0].start)
        self.assertEqual(1.0, first.segments[0].end)
        self.assertEqual("Hello", first.segments[0].text)
        self.assertEqual("Hello world.\n", first.transcript_path.read_text(encoding="utf-8"))

        metadata = json.loads(first.metadata_path.read_text(encoding="utf-8"))
        self.assertEqual(self.audio_path.name, metadata["source_audio_filename"])
        self.assertEqual(
            hashlib.sha256(self.audio_bytes).hexdigest(),
            metadata["source_audio_sha256"],
        )
        self.assertEqual("Hello world.", metadata["transcript"])
        self.assertEqual("en", metadata["detected_language"])
        self.assertEqual(0.975, metadata["language_probability"])
        self.assertEqual(2.75, metadata["duration"])
        self.assertEqual(
            [
                {"start": 0.1, "end": 1.0, "text": "Hello"},
                {"start": 1.1, "end": 2.4, "text": "world."},
            ],
            metadata["segments"],
        )
        self.assertEqual("base.en", metadata["model_name"])
        self.assertEqual("cpu", metadata["device"])
        self.assertEqual("int8", metadata["compute_type"])
        self.assertEqual(6, metadata["cpu_threads"])
        self.assertTrue(metadata["created_at_utc"].endswith("Z"))
        created_at = datetime.fromisoformat(
            metadata["created_at_utc"].replace("Z", "+00:00")
        )
        self.assertIsNotNone(created_at.utcoffset())
        self.assertTrue(second.transcript_path.is_file())
        output_files = tuple(self.output_dir.iterdir())
        self.assertEqual(4, len(output_files))
        self.assertEqual({".json", ".txt"}, {path.suffix for path in output_files})

    def test_missing_model_error_is_clear_and_writes_no_outputs(self) -> None:
        fake_model_class = mock.Mock(
            side_effect=FileNotFoundError("model weights are missing")
        )

        with self.assertRaisesRegex(
            TranscriptionError, "Could not load Faster-Whisper model 'base.en'.*missing"
        ):
            transcription_module.transcribe_audio(
                self.audio_path,
                output_dir=self.output_dir,
                model_dir=self.model_dir,
                whisper_model_class=fake_model_class,
            )

        self.assertEqual([], list(self.output_dir.glob("*.txt")))
        self.assertEqual([], list(self.output_dir.glob("*.json")))

    def test_deferred_generator_failure_is_clear_and_writes_no_outputs(self) -> None:
        def failing_segments():
            yield SimpleNamespace(start=0.0, end=0.5, text="Partial")
            raise RuntimeError("decoder failed during iteration")

        fake_model = SimpleNamespace(
            transcribe=mock.Mock(return_value=(failing_segments(), self._info()))
        )
        fake_model_class = mock.Mock(return_value=fake_model)

        with self.assertRaisesRegex(
            TranscriptionError, "transcription failed.*decoder failed during iteration"
        ):
            transcription_module.transcribe_audio(
                self.audio_path,
                output_dir=self.output_dir,
                model_dir=self.model_dir,
                whisper_model_class=fake_model_class,
            )

        self.assertEqual([], list(self.output_dir.glob("*.txt")))
        self.assertEqual([], list(self.output_dir.glob("*.json")))

    def test_empty_decoded_speech_is_reported_clearly(self) -> None:
        fake_model = SimpleNamespace(
            transcribe=mock.Mock(
                return_value=(
                    iter([SimpleNamespace(start=0.0, end=0.5, text="   ")]),
                    self._info(),
                )
            )
        )

        with self.assertRaisesRegex(TranscriptionError, "no speech.*empty or silent"):
            transcription_module.transcribe_audio(
                self.audio_path,
                output_dir=self.output_dir,
                model_dir=self.model_dir,
                whisper_model_class=mock.Mock(return_value=fake_model),
            )

    def test_shutdown_releases_model_and_is_terminal(self) -> None:
        exhausted: list[bool] = []
        fake_model = SimpleNamespace(
            transcribe=mock.Mock(
                return_value=(self._segments(exhausted), self._info())
            )
        )
        fake_model_class = mock.Mock(return_value=fake_model)
        transcription_module.transcribe_audio(
            self.audio_path,
            output_dir=self.output_dir,
            model_dir=self.model_dir,
            whisper_model_class=fake_model_class,
        )

        transcription_module.shutdown_transcription_runtime()

        self.assertIsNone(transcription_module._whisper_model)
        self.assertIsNone(transcription_module._loaded_runtime_config)
        self.assertIs(transcription_module._runtime_shutdown, True)
        second_audio = self.runtime_root / "after_shutdown.mp3"
        second_audio.write_bytes(b"more fake audio")
        with self.assertRaisesRegex(TranscriptionError, "Start a fresh process"):
            transcription_module.transcribe_audio(
                second_audio,
                output_dir=self.output_dir,
                model_dir=self.model_dir,
                whisper_model_class=fake_model_class,
            )
        fake_model_class.assert_called_once()


if __name__ == "__main__":
    unittest.main()
