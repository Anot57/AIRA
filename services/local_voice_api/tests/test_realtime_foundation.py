"""Unit checks for readiness, protocol validation, and realtime session state."""

from __future__ import annotations

import asyncio
import json
import struct
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

SERVICE_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = SERVICE_ROOT / "src"
sys.path.insert(0, str(SOURCE_ROOT))

import local_voice_api.realtime_session as session_module  # noqa: E402
from local_voice_api.readiness import (  # noqa: E402
    ComponentStatus,
    ReadinessRegistry,
    ReadinessStatus,
    WarmupCoordinator,
    WarmupStep,
    component_is_ready,
)
from local_voice_api.realtime_protocol import (  # noqa: E402
    AI_DISCLOSURE,
    MAX_AUDIO_CHUNK_BYTES,
    PROTOCOL_VERSION,
    ClientEventType,
    PcmAudioFormat,
    ProtocolError,
    ServerEventType,
    SessionStartEvent,
    error_event,
    parse_client_event,
    server_event,
)
from local_voice_api.realtime_session import (  # noqa: E402
    MIN_SPEECH_AUDIO_BYTES,
    RealtimeSession,
    RealtimeSessionState,
)


def _session_start_payload(
    *, companion: str = "aanya", version: int = PROTOCOL_VERSION
) -> str:
    return json.dumps(
        {
            "type": "session_start",
            "protocol_version": version,
            "companion": companion,
            "audio_format": {
                "encoding": "pcm_s16le",
                "sample_rate_hz": 16_000,
                "channels": 1,
            },
        }
    )


def _meaningful_pcm(byte_count: int = MIN_SPEECH_AUDIO_BYTES) -> bytes:
    if byte_count % 2:
        raise ValueError("PCM byte count must contain complete samples")
    return struct.pack("<h", 1000) * (byte_count // 2)


class ReadinessRegistryTests(unittest.TestCase):
    def test_readiness_transitions_are_component_specific_and_safe(self) -> None:
        registry = ReadinessRegistry(("stt", "llm", "tts"))

        self.assertEqual(ReadinessStatus.STARTING, registry.status)
        self.assertEqual(
            {
                "status": "starting",
                "components": {
                    "stt": "not_loaded",
                    "llm": "not_loaded",
                    "tts": "not_loaded",
                },
                "message": "Optional model warmup has not started.",
            },
            registry.snapshot(),
        )

        registry.begin_warmup()
        for component in ("stt", "llm", "tts"):
            registry.set_component(component, ComponentStatus.READY)
        registry.finish_ready()

        snapshot = registry.snapshot()
        self.assertEqual("ready", snapshot["status"])
        self.assertTrue(component_is_ready(snapshot, "tts"))
        self.assertNotIn("/mnt/e/", json.dumps(snapshot))

    def test_readiness_rejects_invalid_components_and_false_completion(self) -> None:
        with self.assertRaises(ValueError):
            ReadinessRegistry(())
        with self.assertRaises(ValueError):
            ReadinessRegistry(("stt", "stt"))

        registry = ReadinessRegistry(("stt", "llm"))
        registry.set_component("stt", ComponentStatus.READY)
        with self.assertRaisesRegex(RuntimeError, "All components"):
            registry.finish_ready()
        with self.assertRaises(KeyError):
            registry.set_component("unknown", ComponentStatus.READY)

    def test_failed_and_degraded_states_remain_observable(self) -> None:
        registry = ReadinessRegistry(("stt", "tts"))
        registry.begin_warmup()
        registry.finish_failed("tts")

        failed = registry.snapshot()
        self.assertEqual("failed", failed["status"])
        self.assertEqual("failed", failed["components"]["tts"])

        registry.mark_degraded("A safe operator-facing summary." + "x" * 300)
        degraded = registry.snapshot()
        self.assertEqual("degraded", degraded["status"])
        self.assertLessEqual(len(degraded["message"]), 160)

        registry.mark_degraded(
            "failure at /mnt/e/aira-local-runtime/models/private-model.bin"
        )
        redacted = registry.snapshot()
        self.assertEqual("The local model runtime is degraded.", redacted["message"])
        self.assertNotIn("/mnt/", redacted["message"])


class WarmupCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_runs_each_step_off_loop_and_marks_ready(self) -> None:
        registry = ReadinessRegistry(("stt", "llm", "tts"))
        event_loop_thread = threading.get_ident()
        callback_threads: list[int] = []

        def mark_callback() -> None:
            callback_threads.append(threading.get_ident())

        coordinator = WarmupCoordinator(
            registry,
            (
                WarmupStep("stt", mark_callback),
                WarmupStep("llm", mark_callback),
                WarmupStep("tts", mark_callback),
            ),
        )

        await coordinator.wait()

        self.assertEqual("ready", registry.snapshot()["status"])
        self.assertEqual(3, len(callback_threads))
        self.assertTrue(
            all(thread_id != event_loop_thread for thread_id in callback_threads)
        )

    async def test_concurrent_start_is_single_flight(self) -> None:
        registry = ReadinessRegistry(("tts",))
        release = threading.Event()
        entered = threading.Event()
        call_count = 0

        def blocking_warmup() -> None:
            nonlocal call_count
            call_count += 1
            entered.set()
            release.wait(timeout=2)

        coordinator = WarmupCoordinator(
            registry, (WarmupStep("tts", blocking_warmup),)
        )

        first, second, third = await asyncio.gather(
            coordinator.start(), coordinator.start(), coordinator.start()
        )
        self.assertIs(first, second)
        self.assertIs(second, third)
        self.assertTrue(await asyncio.to_thread(entered.wait, 1))
        release.set()
        await first

        self.assertEqual(1, call_count)
        self.assertEqual("ready", registry.snapshot()["status"])

    async def test_failure_is_visible_and_stops_later_warmup_steps(self) -> None:
        registry = ReadinessRegistry(("stt", "llm", "tts"))
        later = mock.Mock()

        def fail() -> None:
            raise RuntimeError("private model failure")

        coordinator = WarmupCoordinator(
            registry,
            (
                WarmupStep("stt", lambda: None),
                WarmupStep("llm", fail),
                WarmupStep("tts", later),
            ),
        )

        with mock.patch("local_voice_api.readiness._LOGGER.exception"):
            await coordinator.wait()

        snapshot = registry.snapshot()
        self.assertEqual("failed", snapshot["status"])
        self.assertEqual("ready", snapshot["components"]["stt"])
        self.assertEqual("failed", snapshot["components"]["llm"])
        self.assertEqual("not_loaded", snapshot["components"]["tts"])
        self.assertNotIn("private model failure", json.dumps(snapshot))
        later.assert_not_called()

    async def test_close_waits_for_active_warmup_before_shutdown(self) -> None:
        registry = ReadinessRegistry(("tts",))
        started = threading.Event()

        def warmup() -> None:
            started.set()
            threading.Event().wait(timeout=0.25)

        coordinator = WarmupCoordinator(
            registry, (WarmupStep("tts", warmup),)
        )
        task = await coordinator.start()
        self.assertTrue(await asyncio.to_thread(started.wait, 1))

        await coordinator.close()

        self.assertTrue(task.done())
        self.assertFalse(task.cancelled())
        self.assertEqual("ready", registry.snapshot()["status"])
        with self.assertRaisesRegex(RuntimeError, "closed"):
            await coordinator.start()


class RealtimeProtocolTests(unittest.TestCase):
    def test_session_start_parses_exact_v1_aanya_pcm_contract(self) -> None:
        event = parse_client_event(_session_start_payload())

        self.assertIsInstance(event, SessionStartEvent)
        self.assertEqual(ClientEventType.SESSION_START, event.type)
        self.assertEqual("aanya", event.companion)
        self.assertEqual(PcmAudioFormat(), event.audio_format)
        self.assertIn("adult fictional AI companion", AI_DISCLOSURE)
        self.assertIn("not a human", AI_DISCLOSURE)

    def test_control_events_are_strict_and_binary_audio_is_required(self) -> None:
        for event_type in (
            "end_of_turn",
            "cancel_turn",
            "session_end",
        ):
            with self.subTest(event_type=event_type):
                event = parse_client_event(
                    json.dumps(
                        {"type": event_type, "protocol_version": PROTOCOL_VERSION}
                    )
                )
                self.assertEqual(event_type, event.type.value)

        with self.assertRaises(ProtocolError) as raised:
            parse_client_event(
                json.dumps(
                    {"type": "audio_chunk", "protocol_version": PROTOCOL_VERSION}
                )
            )
        self.assertEqual("binary_audio_required", raised.exception.code)

    def test_protocol_version_and_companion_fail_fatally(self) -> None:
        for payload, expected_code in (
            (_session_start_payload(version=2), "unsupported_protocol_version"),
            (_session_start_payload(companion="tara"), "unsupported_companion"),
        ):
            with self.subTest(expected_code=expected_code):
                with self.assertRaises(ProtocolError) as raised:
                    parse_client_event(payload)
                self.assertEqual(expected_code, raised.exception.code)
                self.assertTrue(raised.exception.fatal)

    def test_malformed_unknown_and_extra_fields_are_recoverable(self) -> None:
        cases = (
            ("not-json", "invalid_json"),
            ("[]", "invalid_message"),
            (
                json.dumps(
                    {"type": "unknown", "protocol_version": PROTOCOL_VERSION}
                ),
                "unknown_event",
            ),
            (
                json.dumps(
                    {
                        "type": "end_of_turn",
                        "protocol_version": PROTOCOL_VERSION,
                        "unexpected": True,
                    }
                ),
                "invalid_event",
            ),
        )
        for payload, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                with self.assertRaises(ProtocolError) as raised:
                    parse_client_event(payload)
                self.assertEqual(expected_code, raised.exception.code)
                self.assertFalse(raised.exception.fatal)

    def test_audio_format_requires_exact_values_and_integer_types(self) -> None:
        invalid_formats = (
            {
                "encoding": "float32",
                "sample_rate_hz": 16_000,
                "channels": 1,
            },
            {
                "encoding": "pcm_s16le",
                "sample_rate_hz": 48_000,
                "channels": 1,
            },
            {
                "encoding": "pcm_s16le",
                "sample_rate_hz": 16_000,
                "channels": 2,
            },
            {
                "encoding": "pcm_s16le",
                "sample_rate_hz": 16_000,
                "channels": True,
            },
        )
        for value in invalid_formats:
            with self.subTest(value=value):
                with self.assertRaises(ProtocolError):
                    PcmAudioFormat.parse(value)

    def test_text_messages_are_bounded_and_must_be_utf8(self) -> None:
        with self.assertRaises(ProtocolError) as invalid_utf8:
            parse_client_event(b"\xff")
        self.assertEqual("invalid_json", invalid_utf8.exception.code)

        oversized = " " * (16 * 1024 + 1)
        with self.assertRaises(ProtocolError) as too_large:
            parse_client_event(oversized)
        self.assertEqual("message_too_large", too_large.exception.code)

    def test_server_and_error_events_include_protocol_version(self) -> None:
        ready = server_event(
            ServerEventType.SESSION_READY,
            session_id="session_test",
            ai_disclosure=AI_DISCLOSURE,
        )
        recoverable = error_event(ProtocolError("no_speech", "Try again."))
        fatal = error_event(
            ProtocolError("unsupported_companion", "Aanya only.", fatal=True)
        )

        self.assertEqual(PROTOCOL_VERSION, ready["protocol_version"])
        self.assertEqual("session_ready", ready["type"])
        self.assertEqual("recoverable_error", recoverable["type"])
        self.assertEqual("fatal_error", fatal["type"])


class RealtimeSessionTests(unittest.TestCase):
    def _listening_session(self) -> RealtimeSession:
        session = RealtimeSession("session_test")
        session.start("aanya", PcmAudioFormat())
        session.begin_listening()
        return session

    def test_happy_path_has_explicit_transitions_and_reuses_session(self) -> None:
        session = self._listening_session()
        session.push_audio(_meaningful_pcm())

        turn = session.finish_audio()

        self.assertRegex(turn.turn_id, r"\Aaanya_rt_[0-9a-f]{32}\Z")
        self.assertEqual(MIN_SPEECH_AUDIO_BYTES, len(turn.audio))
        self.assertEqual(RealtimeSessionState.FINALIZING_STT, session.state)
        self.assertEqual(turn.turn_id, session.active_turn_id)

        session.mark_thinking()
        session.mark_speaking()
        session.complete_turn()

        self.assertEqual(RealtimeSessionState.LISTENING, session.state)
        self.assertIsNone(session.active_turn_id)
        self.assertEqual(0, session.buffered_audio_bytes)

    def test_duplicate_end_of_turn_and_turn_while_busy_are_rejected(self) -> None:
        session = self._listening_session()
        session.push_audio(_meaningful_pcm())
        session.finish_audio()

        for action in (session.finish_audio, session.begin_listening):
            with self.subTest(action=action.__name__):
                with self.assertRaises(ProtocolError) as raised:
                    action()
                self.assertEqual("invalid_transition", raised.exception.code)

    def test_end_of_turn_timing_starts_before_audio_validation(self) -> None:
        session = self._listening_session()
        session.push_audio(_meaningful_pcm())
        metrics = mock.Mock()

        def validate_audio(_audio: bytes | bytearray) -> bool:
            metrics.start_end_of_turn.assert_called_once_with()
            return True

        with mock.patch.object(
            session_module, "RealtimeLatencyMetrics", return_value=metrics
        ), mock.patch.object(
            session_module,
            "_contains_meaningful_audio",
            side_effect=validate_audio,
        ):
            turn = session.finish_audio()

        self.assertIs(metrics, turn.metrics)

    def test_cancel_releases_turn_buffer_and_can_recover(self) -> None:
        session = self._listening_session()
        session.push_audio(_meaningful_pcm())
        turn = session.finish_audio()
        session.mark_thinking()
        cancelled_event = turn.cancel_event

        cancelled_turn = session.cancel_turn()

        self.assertEqual(turn.turn_id, cancelled_turn)
        self.assertTrue(session.cancel_requested.is_set())
        self.assertEqual(RealtimeSessionState.CANCELLED, session.state)
        self.assertEqual(0, session.buffered_audio_bytes)

        session.recover_turn()
        self.assertEqual(RealtimeSessionState.LISTENING, session.state)
        self.assertFalse(session.cancel_requested.is_set())
        self.assertTrue(cancelled_event.is_set())
        self.assertIsNot(cancelled_event, session.cancel_requested)
        self.assertIsNone(session.active_turn_id)

    def test_disconnect_cleanup_is_terminal_and_idempotent(self) -> None:
        session = self._listening_session()
        session.push_audio(_meaningful_pcm())

        session.close()
        session.close()

        self.assertEqual(RealtimeSessionState.CLOSED, session.state)
        self.assertTrue(session.cancel_requested.is_set())
        self.assertEqual(0, session.buffered_audio_bytes)
        self.assertIsNone(session.active_turn_id)
        with self.assertRaises(ProtocolError):
            session.push_audio(b"\x00\x00")

    def test_audio_chunks_and_total_buffer_are_bounded(self) -> None:
        session = self._listening_session()
        with self.assertRaises(ProtocolError) as chunk_error:
            session.push_audio(b"\x00" * (MAX_AUDIO_CHUNK_BYTES + 2))
        self.assertEqual("audio_chunk_too_large", chunk_error.exception.code)

        with mock.patch.object(session_module, "MAX_TURN_AUDIO_BYTES", 8):
            session.push_audio(b"\x01\x00" * 4)
            with self.assertRaises(ProtocolError) as turn_error:
                session.push_audio(b"\x01\x00")
        self.assertEqual("turn_audio_too_large", turn_error.exception.code)
        self.assertEqual(0, session.buffered_audio_bytes)

    def test_empty_short_and_silent_audio_are_recoverable_no_speech(self) -> None:
        cases = (
            b"",
            _meaningful_pcm(MIN_SPEECH_AUDIO_BYTES - 2),
            b"\x00\x00" * (MIN_SPEECH_AUDIO_BYTES // 2),
        )
        for audio in cases:
            with self.subTest(byte_count=len(audio)):
                session = self._listening_session()
                if audio:
                    session.push_audio(audio)
                with self.assertRaises(ProtocolError) as raised:
                    session.finish_audio()
                self.assertEqual("no_speech", raised.exception.code)
                self.assertFalse(raised.exception.fatal)
                self.assertEqual(RealtimeSessionState.LISTENING, session.state)
                self.assertEqual(0, session.buffered_audio_bytes)

    def test_invalid_transitions_and_non_aanya_are_never_silently_routed(self) -> None:
        session = RealtimeSession("session_test")
        with self.assertRaises(ProtocolError) as unsupported:
            session.start("tara", PcmAudioFormat())
        self.assertEqual("unsupported_companion", unsupported.exception.code)
        self.assertTrue(unsupported.exception.fatal)
        self.assertEqual(RealtimeSessionState.CONNECTING, session.state)

        with self.assertRaises(ProtocolError) as invalid:
            session.begin_listening()
        self.assertEqual("invalid_transition", invalid.exception.code)


if __name__ == "__main__":
    unittest.main()
