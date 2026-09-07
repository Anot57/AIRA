"""Lightweight HTTP API checks with the conversation runtime fully mocked."""

from __future__ import annotations

import builtins
import io
import re
import subprocess
import sys
import tempfile
import unittest
import wave
import warnings
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from starlette.exceptions import StarletteDeprecationWarning

with warnings.catch_warnings():
    warnings.simplefilter("ignore", StarletteDeprecationWarning)
    from fastapi.testclient import TestClient

SERVICE_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = SERVICE_ROOT / "src"
sys.path.insert(0, str(SOURCE_ROOT))

import local_voice_api.api as api_module  # noqa: E402
import local_voice_api.api_cli as api_cli  # noqa: E402
from local_voice_api.conversation import (  # noqa: E402
    ConversationError,
    ConversationTurnResult,
)
from local_voice_api.transcription import NoSpeechError  # noqa: E402

FORBIDDEN_RUNTIME_IMPORTS = {
    "cuda",
    "faster_whisper",
    "flash_attn",
    "llama_cpp",
    "qwen_tts",
    "soundfile",
    "torch",
    "transformers",
}
_REAL_IMPORT = builtins.__import__
_IMPORT_PATCHER = None
_POPEN_PATCHER = None


def _minimal_wav_bytes(frames: bytes = b"\x00\x00") -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(24_000)
        wav_file.writeframes(frames)
    return output.getvalue()


INPUT_WAV_BYTES = _minimal_wav_bytes(b"\x00\x00")
OUTPUT_WAV_BYTES = _minimal_wav_bytes(b"\x01\x00\x02\x00")


def setUpModule() -> None:
    """Fail if an API test imports or launches a model runtime."""

    global _IMPORT_PATCHER
    global _POPEN_PATCHER

    imported = FORBIDDEN_RUNTIME_IMPORTS.intersection(sys.modules)
    if imported:
        raise AssertionError(
            f"API tests eagerly imported runtime dependencies: {imported}"
        )

    def guarded_import(name: str, *args: object, **kwargs: object) -> object:
        if name.split(".", maxsplit=1)[0] in FORBIDDEN_RUNTIME_IMPORTS:
            raise AssertionError(f"Lightweight API test attempted to import {name}")
        return _REAL_IMPORT(name, *args, **kwargs)

    _IMPORT_PATCHER = mock.patch(
        "builtins.__import__",
        side_effect=guarded_import,
    )
    _POPEN_PATCHER = mock.patch(
        "subprocess.Popen",
        side_effect=AssertionError(
            "Lightweight API test attempted to launch a process"
        ),
    )
    _IMPORT_PATCHER.start()
    _POPEN_PATCHER.start()


def tearDownModule() -> None:
    if _POPEN_PATCHER is not None:
        _POPEN_PATCHER.stop()
    if _IMPORT_PATCHER is not None:
        _IMPORT_PATCHER.stop()

    imported = FORBIDDEN_RUNTIME_IMPORTS.intersection(sys.modules)
    if imported:
        raise AssertionError(f"API tests imported runtime dependencies: {imported}")


class LocalVoiceApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(
            prefix="api_test_",
            dir=SERVICE_ROOT,
        )
        self.addCleanup(self.temporary_directory.cleanup)
        self.runtime_root = Path(self.temporary_directory.name).resolve()
        self.runtime_root_patcher = mock.patch.object(
            api_module,
            "DEFAULT_RUNTIME_ROOT",
            self.runtime_root,
        )
        self.runtime_root_patcher.start()
        self.addCleanup(self.runtime_root_patcher.stop)
        self.runner = mock.Mock(side_effect=self._successful_conversation)

    def _successful_conversation(self, **kwargs: object) -> ConversationTurnResult:
        turn_id = str(kwargs["turn_id"])
        turn_dir = (
            self.runtime_root
            / "generated"
            / "conversations"
            / turn_id
        )
        audio_dir = turn_dir / "audio"
        audio_dir.mkdir(parents=True)
        metadata_path = turn_dir / "turn.json"
        output_wav_path = audio_dir / "aanya_response.wav"
        metadata_path.write_text("{}\n", encoding="utf-8")
        output_wav_path.write_bytes(OUTPUT_WAV_BYTES)
        return ConversationTurnResult(
            metadata_path=metadata_path,
            output_wav_path=output_wav_path,
            raw_transcript="Hello Anna.",
            normalized_transcript="Hello Aanya.",
            assistant_response="Hello. It's good to hear from you.",
        )

    def _client(
        self,
        *,
        runner: object | None = None,
        max_upload_bytes: int = api_module.MAX_UPLOAD_BYTES,
    ) -> TestClient:
        application = api_module.create_app(
            conversation_runner=self.runner if runner is None else runner,
            max_upload_bytes=max_upload_bytes,
            shutdown_callback=None,
        )
        return TestClient(application)

    def _post_wav(
        self,
        client: TestClient,
        *,
        companion: str = "aanya",
        filename: str = "microphone.wav",
        content: bytes = INPUT_WAV_BYTES,
        content_type: str = "audio/wav",
    ):
        return client.post(
            "/v1/conversation/turn",
            data={"companion": companion},
            files={"audio": (filename, content, content_type)},
        )

    def _assert_invalid_audio(self, response: object) -> None:
        self.assertEqual(400, response.status_code)
        payload = response.json()
        self.assertEqual("invalid_audio", payload["error"])
        self.assertIsInstance(payload["detail"], str)
        self.assertTrue(payload["detail"])

    def test_health_is_exact_and_does_not_call_conversation(self) -> None:
        with self._client() as client:
            response = client.get("/health")

        self.assertEqual(200, response.status_code)
        self.assertEqual(
            {"status": "ok", "service": "aira-local-voice-api"},
            response.json(),
        )
        self.runner.assert_not_called()

    def test_successful_upload_uses_safe_filename_and_exact_runner_kwargs(self) -> None:
        supplied_filename = "../../repository/should-not-be-used.WAV"
        with self._client() as client:
            response = self._post_wav(client, filename=supplied_filename)

        self.assertEqual(200, response.status_code)
        payload = response.json()
        turn_id = payload["turn_id"]
        self.assertRegex(turn_id, r"\Aaanya_turn_[0-9a-f]{32}\Z")
        self.assertEqual(
            {
                "ai_disclosure": (
                    "Aanya is an adult fictional AI companion, not a human."
                ),
                "turn_id": turn_id,
                "companion": "aanya",
                "raw_transcript": "Hello Anna.",
                "normalized_transcript": "Hello Aanya.",
                "response": "Hello. It's good to hear from you.",
                "audio_url": f"/v1/conversation/turns/{turn_id}/audio",
            },
            payload,
        )

        self.runner.assert_called_once()
        self.assertEqual((), self.runner.call_args.args)
        runner_arguments = self.runner.call_args.kwargs
        self.assertEqual(
            {"companion_id", "audio_path", "runtime_root", "turn_id"},
            set(runner_arguments),
        )
        self.assertEqual("aanya", runner_arguments["companion_id"])
        self.assertEqual(self.runtime_root, runner_arguments["runtime_root"])
        self.assertEqual(turn_id, runner_arguments["turn_id"])

        uploaded_path = Path(runner_arguments["audio_path"])
        self.assertEqual(self.runtime_root / "incoming", uploaded_path.parent)
        self.assertRegex(uploaded_path.name, r"\Aupload_[0-9a-f]{32}\.wav\Z")
        self.assertNotIn("repository", uploaded_path.name)
        self.assertNotIn("should-not-be-used", uploaded_path.name)
        self.assertEqual(INPUT_WAV_BYTES, uploaded_path.read_bytes())

    def test_invalid_companion_is_rejected_before_runner(self) -> None:
        with self._client() as client:
            response = self._post_wav(client, companion="tara")

        self.assertEqual(400, response.status_code)
        self.assertEqual("invalid_companion", response.json()["error"])
        self.runner.assert_not_called()
        self.assertEqual([], list((self.runtime_root / "incoming").iterdir()))

    def test_unsupported_extension_media_type_signature_and_empty_file(self) -> None:
        cases = (
            ("turn.txt", b"not audio", "text/plain"),
            ("turn.wav", INPUT_WAV_BYTES, "application/octet-stream"),
            ("turn.wav", b"RIFFbad-data", "audio/wav"),
            ("turn.wav", b"", "audio/wav"),
        )
        with self._client() as client:
            for filename, content, content_type in cases:
                with self.subTest(
                    filename=filename,
                    content_type=content_type,
                    byte_count=len(content),
                ):
                    response = self._post_wav(
                        client,
                        filename=filename,
                        content=content,
                        content_type=content_type,
                    )
                    self._assert_invalid_audio(response)

        self.runner.assert_not_called()
        self.assertEqual([], list((self.runtime_root / "incoming").iterdir()))

    def test_injected_upload_limit_rejects_and_removes_partial_file(self) -> None:
        with self._client(max_upload_bytes=len(INPUT_WAV_BYTES) - 1) as client:
            response = self._post_wav(client)

        self._assert_invalid_audio(response)
        self.runner.assert_not_called()
        self.assertEqual([], list((self.runtime_root / "incoming").iterdir()))

    def test_generated_audio_download_is_no_store(self) -> None:
        with self._client() as client:
            turn_response = self._post_wav(client)
            audio_response = client.get(turn_response.json()["audio_url"])

        turn_id = turn_response.json()["turn_id"]
        self.assertEqual(200, audio_response.status_code)
        self.assertEqual(OUTPUT_WAV_BYTES, audio_response.content)
        self.assertEqual("audio/wav", audio_response.headers["content-type"])
        self.assertEqual("no-store", audio_response.headers["cache-control"])
        self.assertEqual(
            "nosniff",
            audio_response.headers["x-content-type-options"],
        )
        self.assertIn(
            f'filename="{turn_id}.wav"',
            audio_response.headers["content-disposition"],
        )

    def test_nonexistent_turn_audio_returns_safe_404(self) -> None:
        with self._client() as client:
            response = client.get(
                "/v1/conversation/turns/"
                "aanya_turn_00000000000000000000000000000000/audio"
            )

        self.assertEqual(404, response.status_code)
        self.assertEqual(
            {
                "error": "audio_not_found",
                "detail": "The requested conversation audio is unavailable.",
            },
            response.json(),
        )
        self.runner.assert_not_called()

    def test_url_encoded_path_traversal_is_rejected(self) -> None:
        with self._client() as client:
            response = client.get("/v1/conversation/turns/%2e%2e/audio")

        self.assertEqual(404, response.status_code)
        self.assertNotIn(str(self.runtime_root), response.text)
        self.runner.assert_not_called()

    def test_conversation_error_returns_generic_500_without_path_leak(self) -> None:
        private_detail = (
            "Qwen failed at /mnt/e/aira-local-runtime/llama-cache/private.gguf"
        )
        failing_runner = mock.Mock(
            side_effect=ConversationError(private_detail)
        )
        with mock.patch.object(api_module._LOGGER, "exception") as logged:
            with self._client(runner=failing_runner) as client:
                response = self._post_wav(client)

        self.assertEqual(500, response.status_code)
        self.assertEqual("conversation_failed", response.json()["error"])
        self.assertNotIn(private_detail, response.text)
        self.assertNotIn("private.gguf", response.text)
        self.assertNotIn("/mnt/e/", response.text)
        logged.assert_called_once()
        failing_runner.assert_called_once()
        self.assertEqual([], list((self.runtime_root / "incoming").iterdir()))

    def test_known_no_speech_is_typed_and_recoverable_without_path_leak(self) -> None:
        no_speech_runner = mock.Mock(
            side_effect=NoSpeechError(
                "private decoder detail at /mnt/e/aira-local-runtime/input.wav"
            )
        )
        with self._client(runner=no_speech_runner) as client:
            response = self._post_wav(client)

        self.assertEqual(422, response.status_code)
        self.assertEqual("no_speech", response.json()["error"])
        self.assertIn("try again", response.json()["detail"].casefold())
        self.assertNotIn("/mnt/e/", response.text)
        no_speech_runner.assert_called_once()
        self.assertEqual([], list((self.runtime_root / "incoming").iterdir()))

    def test_http_turn_emits_bounded_stage_timing_without_content(self) -> None:
        with self.assertLogs("local_voice_api.observability", level="INFO") as logs:
            with self._client() as client:
                response = self._post_wav(client)

        self.assertEqual(200, response.status_code)
        rendered = "\n".join(logs.output)
        for stage in (
            "request_received",
            "multipart_parse",
            "upload_validation",
            "upload_write",
            "audio_ready",
            "conversation_pipeline",
            "result_validation",
            "response_json_construction",
            "http_request_total",
        ):
            self.assertIn(f"stage={stage}", rendered)
        self.assertNotIn("Hello Anna", rendered)
        self.assertNotIn("should-not-be-used", rendered)

    def test_conversation_output_outside_runtime_root_is_rejected(self) -> None:
        outside_temporary_directory = tempfile.TemporaryDirectory(
            prefix="api_outside_",
            dir=SERVICE_ROOT,
        )
        self.addCleanup(outside_temporary_directory.cleanup)
        outside_wav_path = (
            Path(outside_temporary_directory.name).resolve() / "escaped.wav"
        )
        outside_wav_path.write_bytes(OUTPUT_WAV_BYTES)

        def escaped_result(**kwargs: object) -> ConversationTurnResult:
            turn_id = str(kwargs["turn_id"])
            turn_dir = (
                self.runtime_root
                / "generated"
                / "conversations"
                / turn_id
            )
            turn_dir.mkdir(parents=True)
            metadata_path = turn_dir / "turn.json"
            metadata_path.write_text("{}\n", encoding="utf-8")
            return ConversationTurnResult(
                metadata_path=metadata_path,
                output_wav_path=outside_wav_path,
                raw_transcript="Hello Anna.",
                normalized_transcript="Hello Aanya.",
                assistant_response="Hello.",
            )

        escaping_runner = mock.Mock(side_effect=escaped_result)
        with mock.patch.object(api_module._LOGGER, "exception"):
            with self._client(runner=escaping_runner) as client:
                response = self._post_wav(client)

        self.assertEqual(500, response.status_code)
        self.assertEqual("conversation_failed", response.json()["error"])
        self.assertNotIn(str(outside_wav_path), response.text)
        escaping_runner.assert_called_once()
        turn_id = escaping_runner.call_args.kwargs["turn_id"]
        with self._client() as client:
            audio_response = client.get(
                f"/v1/conversation/turns/{turn_id}/audio"
            )
        self.assertEqual(404, audio_response.status_code)
        self.assertEqual([], list((self.runtime_root / "incoming").iterdir()))

    def test_missing_multipart_fields_are_reported_as_400(self) -> None:
        with self._client() as client:
            missing_audio = client.post(
                "/v1/conversation/turn",
                data={"companion": "aanya"},
            )
            missing_companion = client.post(
                "/v1/conversation/turn",
                files={
                    "audio": (
                        "microphone.wav",
                        INPUT_WAV_BYTES,
                        "audio/wav",
                    )
                },
            )

        self.assertEqual(400, missing_audio.status_code)
        self.assertEqual("invalid_audio", missing_audio.json()["error"])
        self.assertEqual(400, missing_companion.status_code)
        self.assertEqual(
            "invalid_companion",
            missing_companion.json()["error"],
        )
        self.runner.assert_not_called()
        self.assertEqual([], list((self.runtime_root / "incoming").iterdir()))


class ApiCliTests(unittest.TestCase):
    def _run_cli(self, arguments: list[str]) -> mock.Mock:
        uvicorn_run = mock.Mock()
        fake_uvicorn = SimpleNamespace(run=uvicorn_run)
        with (
            mock.patch.object(api_cli, "_load_uvicorn", return_value=fake_uvicorn),
            mock.patch.object(api_cli.logging, "basicConfig"),
            mock.patch.object(api_cli.logging, "warning"),
        ):
            exit_code = api_cli.main(arguments)
        self.assertEqual(0, exit_code)
        return uvicorn_run

    def test_cli_uses_local_development_defaults(self) -> None:
        uvicorn_run = self._run_cli([])

        uvicorn_run.assert_called_once_with(
            "local_voice_api.api:app",
            host="0.0.0.0",
            port=8765,
            workers=1,
            reload=False,
        )

    def test_cli_allows_host_and_port_overrides(self) -> None:
        uvicorn_run = self._run_cli(
            ["--host", "127.0.0.1", "--port", "9876"]
        )

        uvicorn_run.assert_called_once_with(
            "local_voice_api.api:app",
            host="127.0.0.1",
            port=9876,
            workers=1,
            reload=False,
        )


if __name__ == "__main__":
    unittest.main()
