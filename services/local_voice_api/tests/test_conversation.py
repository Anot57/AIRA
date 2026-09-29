"""Standard-library checks for one-turn local conversation orchestration."""

from __future__ import annotations

import builtins
import io
import json
import subprocess
import sys
import tempfile
import unittest
import wave
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SERVICE_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = SERVICE_ROOT / "src"
sys.path.insert(0, str(SOURCE_ROOT))

import local_voice_api.conversation as conversation_module  # noqa: E402
import local_voice_api.conversation_cli as conversation_cli  # noqa: E402
from local_voice_api.approved_references import (  # noqa: E402
    get_approved_voice_reference,
)
from local_voice_api.conversation import (  # noqa: E402
    AANYA_SYSTEM_PERSONA,
    LLM_MODEL_FILENAME,
    LLM_MODEL_NAME,
    ConversationError,
    ConversationTurnResult,
    build_llama_command,
    extract_assistant_response,
    generate_local_llm_response,
    get_companion_persona,
    normalize_companion_transcript,
    resolve_cached_llm_model,
)
from local_voice_api.observability import TurnTiming  # noqa: E402
from local_voice_api.synthesis import (  # noqa: E402
    BASE_MODEL_ID as TTS_MODEL_ID,
    DEFAULT_SYNTHESIS_SEED,
    SynthesisResult,
)
from local_voice_api.transcription import (  # noqa: E402
    DEFAULT_CPU_THREADS,
    MODEL_NAME as STT_MODEL_NAME,
    TranscriptSegment,
    TranscriptionResult,
)

FORBIDDEN_RUNTIME_IMPORTS = {
    "cuda",
    "faster_whisper",
    "llama_cpp",
    "qwen_tts",
    "soundfile",
    "torch",
    "transformers",
}
_REAL_IMPORT = builtins.__import__
_IMPORT_PATCHER = None
_POPEN_PATCHER = None


def setUpModule() -> None:
    """Fail immediately if a lightweight test imports or launches a runtime."""

    global _IMPORT_PATCHER
    global _POPEN_PATCHER

    imported = FORBIDDEN_RUNTIME_IMPORTS.intersection(sys.modules)
    if imported:
        raise AssertionError(
            f"Conversation tests eagerly imported runtime dependencies: {imported}"
        )

    def guarded_import(name: str, *args: object, **kwargs: object) -> object:
        if name.split(".", maxsplit=1)[0] in FORBIDDEN_RUNTIME_IMPORTS:
            raise AssertionError(
                f"Lightweight conversation test attempted to import {name}"
            )
        return _REAL_IMPORT(name, *args, **kwargs)

    _IMPORT_PATCHER = mock.patch(
        "builtins.__import__", side_effect=guarded_import
    )
    _POPEN_PATCHER = mock.patch(
        "subprocess.Popen",
        side_effect=AssertionError(
            "Lightweight conversation test attempted to launch a process"
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
        raise AssertionError(
            f"Conversation tests imported runtime dependencies: {imported}"
        )


class ConversationRuntimeFixture(unittest.TestCase):
    """Tiny E-drive fixture with no valid audio or model weights."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(dir=SERVICE_ROOT)
        self.addCleanup(self.temporary_directory.cleanup)
        self.runtime_root = Path(self.temporary_directory.name).resolve()
        runtime_root_patcher = mock.patch.object(
            conversation_module, "DEFAULT_RUNTIME_ROOT", self.runtime_root
        )
        runtime_root_patcher.start()
        self.addCleanup(runtime_root_patcher.stop)

        self.virtual_environment = self.runtime_root / ".venv"
        self.virtual_environment.mkdir()
        self.environment = {"VIRTUAL_ENV": str(self.virtual_environment)}

        self.audio_path = self.runtime_root / "input" / "amman_test.wav"
        self.audio_path.parent.mkdir()
        self.audio_path.write_bytes(b"not real audio")

        self.llama_cli_path = (
            self.runtime_root / "llama" / "llama-b10715" / "llama-cli"
        )
        self.llama_cli_path.parent.mkdir(parents=True)
        self.llama_cli_path.write_bytes(b"not a real executable")

        self.cache_dir = self.runtime_root / "llama-cache"
        repository_root = (
            self.cache_dir / "models--ggml-org--Qwen3-1.7B-GGUF"
        )
        self.revision = "a" * 40
        refs_dir = repository_root / "refs"
        refs_dir.mkdir(parents=True)
        (refs_dir / "main").write_text(
            f"{self.revision}\n", encoding="utf-8"
        )
        self.model_path = (
            repository_root
            / "snapshots"
            / self.revision
            / LLM_MODEL_FILENAME
        )
        self.model_path.parent.mkdir(parents=True)
        self.model_path.write_bytes(b"tiny fake GGUF; never loaded")

        self.stt_model_dir = (
            self.runtime_root / "models" / "faster-whisper"
        )
        self.stt_model_dir.mkdir(parents=True)

        self.reference_dir = (
            self.runtime_root / "generated" / "voices"
        )
        self.reference_dir.mkdir(parents=True)
        self.approved_reference = get_approved_voice_reference("aanya")
        self.reference_wav_path = (
            self.reference_dir / self.approved_reference.wav_filename
        )
        self.reference_metadata_path = (
            self.reference_dir / self.approved_reference.metadata_filename
        )
        with wave.open(str(self.reference_wav_path), "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(24_000)
            wav_file.writeframes(b"\x00\x00")
        self.reference_metadata_path.write_text(
            json.dumps(
                {
                    "adult": True,
                    "ai_generated": True,
                    "companion_id": "aanya",
                    "model_id": "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign",
                    "sample_rate": 24_000,
                    "seed": self.approved_reference.seed,
                    "transcript": "I'm Aanya, an approved adult AI voice.",
                }
            ),
            encoding="utf-8",
        )

        self.output_dir = (
            self.runtime_root / "generated" / "conversations"
        )


class NameNormalizationTests(unittest.TestCase):
    def test_aanya_variants_are_normalized_as_whole_words(self) -> None:
        transcript = (
            "Anna called Anya, then Ana answered; Annabelle brought a banana."
        )

        normalized = normalize_companion_transcript("aanya", transcript)

        self.assertEqual(
            "Aanya called Aanya, then Aanya answered; "
            "Annabelle brought a banana.",
            normalized,
        )

    def test_variants_are_unchanged_for_a_different_active_companion(self) -> None:
        transcript = "Anna, Anya, and Ana are names in this sentence."

        self.assertEqual(
            transcript,
            normalize_companion_transcript("tara", transcript),
        )


class PersonaAndLlamaCommandTests(unittest.TestCase):
    def test_aanya_persona_has_disclosure_and_safety_boundaries(self) -> None:
        persona = get_companion_persona("aanya")
        folded = persona.casefold()

        self.assertEqual(AANYA_SYSTEM_PERSONA, persona)
        self.assertIn("adult fictional ai companion", folded)
        self.assertIn("never a human", folded)
        self.assertIn("warmly", folded)
        self.assertIn("naturally", folded)
        self.assertIn("calmly", folded)
        self.assertIn("one to three sentences", folded)
        self.assertIn("therapist", folded)
        self.assertIn("emergency services", folded)
        self.assertIn("dependency", folded)
        self.assertIn("exclusivity", folded)

    def test_ordinary_turns_forbid_unsolicited_identity_introductions(self) -> None:
        persona = get_companion_persona("aanya").casefold()

        self.assertIn("never introduce yourself", persona)
        self.assertIn("state your name", persona)
        self.assertIn("repeat the ai disclosure", persona)
        self.assertIn("unless the user explicitly asks", persona)
        self.assertIn("do not begin every response with a greeting", persona)
        self.assertIn("respond directly to the user's latest words", persona)
        self.assertIn("what can i do for you", persona)
        self.assertIn("i am here to help", persona)
        self.assertIn("what do you need help with", persona)

    def test_name_question_explicitly_allows_a_truthful_aanya_answer(self) -> None:
        persona = get_companion_persona("aanya").casefold()

        self.assertIn(
            "if asked your name or who you are, you may truthfully say you are aanya",
            persona,
        )

    def test_ai_question_requires_truthful_ai_disclosure(self) -> None:
        persona = get_companion_persona("aanya").casefold()

        self.assertIn(
            "if asked whether you are ai or human, clearly and truthfully say "
            "you are an ai, not a human",
            persona,
        )

    def test_name_introduction_does_not_encourage_parroting(self) -> None:
        persona = get_companion_persona("aanya").casefold()

        self.assertIn("nice to meet you, aman", persona)
        self.assertIn(
            "never repeat their introduction as if it were your own identity",
            persona,
        )

    def test_surprise_reactions_are_limited_to_surprising_context(self) -> None:
        persona = get_companion_persona("aanya").casefold()

        self.assertIn("reserve 'really?' and 'wait, seriously?'", persona)
        self.assertIn("never use either as a generic answer", persona)
        self.assertIn("neutral information request", persona)

    def test_affection_guidance_is_warm_but_non_dependent(self) -> None:
        persona = get_companion_persona("aanya").casefold()

        self.assertIn("if the user says 'i love you'", persona)
        self.assertIn("respond warmly in your own words", persona)
        self.assertIn("care about you too", persona)
        self.assertIn("without claiming to be human, exclusive", persona)

    def test_llama_command_is_offline_non_thinking_and_prompt_clean(self) -> None:
        llama_path = Path("/mnt/e/aira-local-runtime/llama/llama-cli")
        model_path = Path(
            "/mnt/e/aira-local-runtime/llama-cache/Qwen3-1.7B-Q4_K_M.gguf"
        )
        transcript = "Hello Aanya."

        command = build_llama_command(
            llama_cli_path=llama_path,
            model_path=model_path,
            transcript=transcript,
            system_persona=AANYA_SYSTEM_PERSONA,
        )

        def argument_after(flag: str) -> str:
            return command[command.index(flag) + 1]

        self.assertEqual(str(llama_path), command[0])
        self.assertEqual(str(model_path), argument_after("--model"))
        self.assertIn("--offline", command)
        self.assertEqual("off", argument_after("--flash-attn"))
        self.assertIn("--jinja", command)
        self.assertNotIn("--conversation", command)
        self.assertIn("--single-turn", command)
        self.assertEqual("off", argument_after("--reasoning"))
        self.assertEqual("0", argument_after("--reasoning-budget"))
        self.assertEqual(
            AANYA_SYSTEM_PERSONA, argument_after("--system-prompt")
        )
        self.assertEqual(transcript, argument_after("--prompt"))
        self.assertIn("--no-display-prompt", command)
        self.assertIn("--no-show-timings", command)
        self.assertIn("--log-disable", command)
        self.assertNotIn("--hf-repo", command)
        self.assertNotIn("--model-url", command)


class CachedModelResolutionTests(ConversationRuntimeFixture):
    def test_refs_main_selects_the_exact_tiny_cached_gguf(self) -> None:
        decoy = (
            self.model_path.parents[2]
            / "snapshots"
            / ("b" * 40)
            / LLM_MODEL_FILENAME
        )
        decoy.parent.mkdir(parents=True)
        decoy.write_bytes(b"different tiny fake GGUF")

        resolved = resolve_cached_llm_model(
            self.cache_dir,
            runtime_root=self.runtime_root,
        )

        self.assertEqual(self.model_path, resolved)
        self.assertEqual(b"tiny fake GGUF; never loaded", resolved.read_bytes())


class AssistantOutputTests(ConversationRuntimeFixture):
    def test_only_assistant_speech_survives_prompt_labels_and_thinking(self) -> None:
        transcript = "I had a difficult day, Aanya."
        raw_output = (
            f"{AANYA_SYSTEM_PERSONA}\n"
            f"User: {transcript}\n"
            "Assistant:\n"
            "<think>This private reasoning must never be spoken.</think>\n"
            "That sounds exhausting. Want to tell me which part felt hardest?\n"
            "User: This generated continuation must also be removed."
        )

        response = extract_assistant_response(
            raw_output,
            transcript=transcript,
            system_persona=AANYA_SYSTEM_PERSONA,
        )

        self.assertEqual(
            "That sounds exhausting. Want to tell me which part felt hardest?",
            response,
        )
        self.assertNotIn("reasoning", response)
        self.assertNotIn("User:", response)
        self.assertNotIn("Assistant:", response)

    def test_incomplete_thinking_markup_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            ConversationError, "incomplete thinking block"
        ):
            extract_assistant_response(
                "Assistant: <think>private reasoning without an end tag",
                transcript="Hello Aanya.",
                system_persona=AANYA_SYSTEM_PERSONA,
            )

    def test_complete_text_fence_is_unwrapped_without_speaking_language(self) -> None:
        response = extract_assistant_response(
            "Assistant: ```text\nHello, it's good to hear from you.\n```",
            transcript="Hello Aanya.",
            system_persona=AANYA_SYSTEM_PERSONA,
        )

        self.assertEqual("Hello, it's good to hear from you.", response)

    def test_llm_adapter_uses_injected_runner_and_e_drive_cache(self) -> None:
        fake_runner = mock.Mock(
            return_value=SimpleNamespace(
                returncode=0,
                stdout="Assistant: I'm here. What would you like to talk about?",
                stderr="",
            )
        )
        environment = {"SAFE_TEST_VALUE": "kept"}

        response = generate_local_llm_response(
            "Hello Aanya.",
            system_persona=AANYA_SYSTEM_PERSONA,
            llama_cli_path=self.llama_cli_path,
            model_path=self.model_path,
            cache_dir=self.cache_dir,
            environ=environment,
            subprocess_runner=fake_runner,
        )

        self.assertEqual(
            "I'm here. What would you like to talk about?", response
        )
        fake_runner.assert_called_once()
        command = fake_runner.call_args.args[0]
        options = fake_runner.call_args.kwargs
        self.assertIn("--offline", command)
        self.assertEqual("off", command[command.index("--reasoning") + 1])
        self.assertIs(options["shell"], False)
        self.assertIs(options["capture_output"], True)
        self.assertIs(options["check"], False)
        self.assertEqual(str(self.cache_dir), options["env"]["LLAMA_CACHE"])
        self.assertEqual("kept", options["env"]["SAFE_TEST_VALUE"])
        self.assertNotIn("LLAMA_CACHE", environment)


class ConversationPipelineTests(ConversationRuntimeFixture):
    def test_mocked_turn_writes_complete_metadata_and_routes_stage_data(self) -> None:
        raw_transcript = "Hi Anna, can you help me plan tomorrow?"
        normalized_transcript = "Hi Aanya, can you help me plan tomorrow?"
        assistant_response = (
            "Absolutely. Tell me the two most important things for tomorrow."
        )

        def fake_transcribe(**kwargs: object) -> TranscriptionResult:
            output_dir = Path(kwargs["output_dir"])
            output_dir.mkdir(parents=True)
            transcript_path = output_dir / "transcript.txt"
            metadata_path = output_dir / "transcript.json"
            transcript_path.write_text(raw_transcript, encoding="utf-8")
            metadata_path.write_text("{}", encoding="utf-8")
            return TranscriptionResult(
                transcript_path=transcript_path,
                metadata_path=metadata_path,
                transcript=raw_transcript,
                detected_language="en",
                language_probability=0.99,
                duration=2.75,
                segments=(
                    TranscriptSegment(
                        start=0.1,
                        end=2.5,
                        text=raw_transcript,
                    ),
                ),
            )

        def fake_synthesize(**kwargs: object) -> SynthesisResult:
            output_dir = Path(kwargs["output_dir"])
            output_dir.mkdir(parents=True)
            wav_path = output_dir / "aanya_response.wav"
            metadata_path = output_dir / "synthesis.json"
            wav_path.write_bytes(b"not real synthesized audio")
            metadata_path.write_text("{}", encoding="utf-8")
            return SynthesisResult(
                wav_path=wav_path,
                metadata_path=metadata_path,
                sample_rate=24_000,
            )

        transcriber = mock.Mock(side_effect=fake_transcribe)
        captured_llm_environment: dict[str, str] = {}

        def fake_llm(*args: object, **kwargs: object) -> str:
            captured_llm_environment.update(dict(kwargs["environ"]))
            return assistant_response

        llm_runner = mock.Mock(side_effect=fake_llm)
        synthesizer = mock.Mock(side_effect=fake_synthesize)
        turn_id = "aanya_turn_unit_test"
        timing = TurnTiming(turn_id)

        with timing.bind(), mock.patch.dict(
            conversation_module.os.environ, self.environment, clear=True
        ):
            result = conversation_module.run_conversation_turn(
                companion_id="aanya",
                audio_path=self.audio_path,
                runtime_root=self.runtime_root,
                turn_id=turn_id,
                transcriber=transcriber,
                llm_runner=llm_runner,
                synthesizer=synthesizer,
            )

        measured_stages = {measurement.stage for measurement in timing.snapshot()}
        self.assertTrue(
            {
                "stt_total",
                "transcript_normalization",
                "llm_generation",
                "tts_total",
                "turn_metadata_write",
                "conversation_total",
            }.issubset(measured_stages)
        )

        turn_dir = self.output_dir / turn_id
        self.assertEqual(raw_transcript, result.raw_transcript)
        self.assertEqual(normalized_transcript, result.normalized_transcript)
        self.assertEqual(assistant_response, result.assistant_response)
        self.assertEqual(turn_dir / "turn.json", result.metadata_path)
        self.assertEqual(
            turn_dir / "audio" / "aanya_response.wav",
            result.output_wav_path,
        )

        transcriber.assert_called_once_with(
            audio_path=self.audio_path,
            output_dir=turn_dir / "transcription",
            model_dir=self.stt_model_dir,
            cpu_threads=DEFAULT_CPU_THREADS,
        )
        self.assertEqual(normalized_transcript, llm_runner.call_args.args[0])
        self.assertEqual(
            AANYA_SYSTEM_PERSONA,
            llm_runner.call_args.kwargs["system_persona"],
        )
        self.assertEqual(
            self.llama_cli_path,
            llm_runner.call_args.kwargs["llama_cli_path"],
        )
        self.assertEqual(
            self.model_path,
            llm_runner.call_args.kwargs["model_path"],
        )
        self.assertEqual(
            self.cache_dir,
            llm_runner.call_args.kwargs["cache_dir"],
        )
        self.assertEqual(
            str(self.cache_dir), captured_llm_environment["LLAMA_CACHE"]
        )
        self.assertTrue(
            Path(captured_llm_environment["PYTHONPYCACHEPREFIX"]).is_relative_to(
                self.runtime_root
            )
        )
        synthesizer.assert_called_once_with(
            companion_id="aanya",
            text=assistant_response,
            reference_dir=self.reference_dir,
            output_dir=turn_dir / "audio",
            seed=DEFAULT_SYNTHESIS_SEED,
        )

        metadata = json.loads(result.metadata_path.read_text(encoding="utf-8"))
        expected_required_values = {
            "assistant_response": assistant_response,
            "companion": "aanya",
            "input_audio_path": str(self.audio_path),
            "llm_model": LLM_MODEL_NAME,
            "normalized_transcript": normalized_transcript,
            "output_wav_path": str(result.output_wav_path),
            "raw_transcript": raw_transcript,
            "stt_model": STT_MODEL_NAME,
            "tts_reference": str(self.reference_wav_path),
        }
        for field, expected in expected_required_values.items():
            with self.subTest(field=field):
                self.assertEqual(expected, metadata[field])

        self.assertIn("adult fictional AI companion", metadata["ai_disclosure"])
        self.assertEqual(2.75, metadata["durations_seconds"]["input_audio"])
        for stage in ("stt", "llm", "tts", "total"):
            with self.subTest(duration=stage):
                self.assertGreaterEqual(metadata["durations_seconds"][stage], 0)
        self.assertEqual(
            [{"end": 2.5, "start": 0.1, "text": raw_transcript}],
            metadata["transcript_segments"],
        )
        for label, timestamp in metadata["timestamps_utc"].items():
            with self.subTest(timestamp=label):
                self.assertTrue(timestamp.endswith("Z"))
                parsed = datetime.fromisoformat(
                    timestamp.replace("Z", "+00:00")
                )
                self.assertIsNotNone(parsed.utcoffset())

        runtime = metadata["runtime"]
        self.assertIs(runtime["llm_thinking_enabled"], False)
        self.assertEqual("cpu", runtime["stt_device"])
        self.assertEqual("int8", runtime["stt_compute_type"])
        self.assertEqual(TTS_MODEL_ID, runtime["tts_model"])
        self.assertEqual(24_000, runtime["tts_sample_rate"])
        self.assertTrue(result.output_wav_path.is_file())
        self.assertTrue(result.metadata_path.is_file())
        self.assertTrue(
            result.output_wav_path.is_relative_to(self.runtime_root)
        )
        self.assertTrue(result.metadata_path.is_relative_to(self.runtime_root))


class ConversationCliTests(unittest.TestCase):
    def test_cli_reports_one_turn_and_cleans_up_both_runtimes(self) -> None:
        result = ConversationTurnResult(
            metadata_path=Path("/mnt/e/aira-local-runtime/generated/turn.json"),
            output_wav_path=Path(
                "/mnt/e/aira-local-runtime/generated/aanya_response.wav"
            ),
            raw_transcript="Hello Anna.",
            normalized_transcript="Hello Aanya.",
            assistant_response="Hello. It's good to hear from you.",
        )
        output = io.StringIO()

        with mock.patch.object(
            conversation_cli,
            "run_conversation_turn",
            return_value=result,
        ) as run_turn, mock.patch.object(
            conversation_cli, "shutdown_transcription_runtime"
        ) as shutdown_stt, mock.patch.object(
            conversation_cli, "shutdown_voice_clone_runtime"
        ) as shutdown_tts, redirect_stdout(output):
            exit_code = conversation_cli.main(
                [
                    "--companion",
                    "aanya",
                    "--audio",
                    "/mnt/e/aira-local-runtime/input/amman_test.wav",
                ]
            )

        self.assertEqual(0, exit_code)
        run_turn.assert_called_once_with(
            companion_id="aanya",
            audio_path=Path(
                "/mnt/e/aira-local-runtime/input/amman_test.wav"
            ),
            cpu_threads=DEFAULT_CPU_THREADS,
            tts_seed=DEFAULT_SYNTHESIS_SEED,
            llm_timeout_seconds=conversation_module.DEFAULT_LLM_TIMEOUT_SECONDS,
        )
        shutdown_stt.assert_called_once_with()
        shutdown_tts.assert_called_once_with()
        self.assertIn("AI companion: aanya", output.getvalue())
        self.assertIn("Transcript: Hello Aanya.", output.getvalue())
        self.assertIn(f"WAV: {result.output_wav_path}", output.getvalue())
        self.assertIn(
            f"Turn metadata: {result.metadata_path}", output.getvalue()
        )

    def test_cli_cleans_up_both_runtimes_after_failure(self) -> None:
        error_output = io.StringIO()

        with mock.patch.object(
            conversation_cli,
            "run_conversation_turn",
            side_effect=ConversationError("mocked local failure"),
        ), mock.patch.object(
            conversation_cli, "shutdown_transcription_runtime"
        ) as shutdown_stt, mock.patch.object(
            conversation_cli, "shutdown_voice_clone_runtime"
        ) as shutdown_tts, redirect_stderr(error_output):
            with self.assertRaises(SystemExit) as raised:
                conversation_cli.main(
                    [
                        "--companion",
                        "aanya",
                        "--audio",
                        "/mnt/e/aira-local-runtime/input/amman_test.wav",
                    ]
                )

        self.assertEqual(1, raised.exception.code)
        self.assertIn("error: mocked local failure", error_output.getvalue())
        shutdown_stt.assert_called_once_with()
        shutdown_tts.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
