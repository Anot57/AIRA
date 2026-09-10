"""Mock-only integration checks for readiness and the realtime WebSocket."""

from __future__ import annotations

import asyncio
import struct
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

SERVICE_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = SERVICE_ROOT / "src"
sys.path.insert(0, str(SOURCE_ROOT))

import local_voice_api.api as api_module  # noqa: E402
from local_voice_api.realtime_pipeline import StreamingRealtimeTurnProcessor  # noqa: E402
from local_voice_api.readiness import (  # noqa: E402
    ComponentStatus,
    ReadinessRegistry,
    WarmupCoordinator,
    WarmupStep,
)
from local_voice_api.realtime_protocol import (  # noqa: E402
    PcmAudioFormat,
    ServerEventType,
    server_event,
)
from local_voice_api.streaming import (  # noqa: E402
    StreamingTranscript,
    SynthesizedAudioChunk,
)


def _session_start(*, version: int = 1, companion: str = "aanya") -> dict[str, object]:
    return {
        "type": "session_start",
        "protocol_version": version,
        "companion": companion,
        "audio_format": PcmAudioFormat().payload(),
    }


def _control(event_type: str) -> dict[str, object]:
    return {"type": event_type, "protocol_version": 1}


def _meaningful_pcm() -> bytes:
    return struct.pack("<h", 1000) * 4_000


def _ready_registry() -> ReadinessRegistry:
    registry = ReadinessRegistry(api_module.READINESS_COMPONENTS)
    registry.begin_warmup()
    for component in api_module.READINESS_COMPONENTS:
        registry.set_component(component, ComponentStatus.READY)
    registry.finish_ready()
    return registry


class _Service:
    def __init__(self, incoming: Path) -> None:
        self.incoming = incoming

    def prepare_incoming_directory(self) -> Path:
        self.incoming.mkdir(parents=True, exist_ok=True)
        return self.incoming

    @staticmethod
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "aira-local-voice-api"}


class _ImmediateProcessor:
    def __init__(self) -> None:
        self.turn_ids: list[str] = []
        self.cancelled = False
        self.closed = False

    async def process_turn(self, turn, sink, cancel_event: threading.Event) -> None:
        self.turn_ids.append(turn.turn_id)
        await sink.send_json(
            server_event(
                ServerEventType.STT_FINAL,
                turn_id=turn.turn_id,
                text="Hello Aanya",
            )
        )
        await sink.send_json(
            server_event(ServerEventType.THINKING, turn_id=turn.turn_id)
        )
        await sink.send_json(
            server_event(ServerEventType.SPEAKING, turn_id=turn.turn_id)
        )
        await sink.send_json(
            server_event(
                ServerEventType.AUDIO_CHUNK,
                turn_id=turn.turn_id,
                sequence=0,
                encoding="pcm_s16le",
                sample_rate_hz=24_000,
                channels=1,
                byte_length=4,
            )
        )
        await sink.send_audio(b"\x01\x00\x02\x00")
        await sink.send_json(
            server_event(
                ServerEventType.TURN_COMPLETE,
                turn_id=turn.turn_id,
                metrics={"ttfa_ms": 1.0},
            )
        )

    async def cancel(self) -> None:
        self.cancelled = True

    async def close(self) -> None:
        self.closed = True


class _BlockingProcessor:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.finished = threading.Event()
        self.cancelled = False
        self.closed = False

    async def process_turn(self, turn, sink, cancel_event: threading.Event) -> None:
        self.started.set()
        try:
            await asyncio.Future()
        finally:
            self.finished.set()

    async def cancel(self) -> None:
        self.cancelled = True

    async def close(self) -> None:
        self.closed = True


class _CancellationRaceProcessor:
    """Attempts stale output while its deliberately slow cancel hook runs."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.cancel_hook_started = threading.Event()
        self.calls = 0
        self.closed = False

    async def process_turn(self, turn, sink, cancel_event: threading.Event) -> None:
        self.calls += 1
        if self.calls == 1:
            self.started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                # A badly behaved adapter must not leak any of these events
                # after the server has accepted cancellation.
                await sink.send_json(
                    server_event(
                        ServerEventType.TEXT_SENTENCE,
                        turn_id=turn.turn_id,
                        sequence=1,
                        text="stale response",
                    )
                )
                await sink.send_json(
                    server_event(
                        ServerEventType.AUDIO_CHUNK,
                        turn_id=turn.turn_id,
                        sequence=0,
                        encoding="pcm_s16le",
                        sample_rate_hz=24_000,
                        channels=1,
                        byte_length=2,
                    )
                )
                await sink.send_audio(b"\x01\x00")
                await sink.send_json(
                    server_event(
                        ServerEventType.TURN_COMPLETE,
                        turn_id=turn.turn_id,
                        metrics={"ttfa_ms": 1.0},
                    )
                )
                return
        await sink.send_json(
            server_event(
                ServerEventType.STT_FINAL,
                turn_id=turn.turn_id,
                text="second turn",
            )
        )
        await sink.send_json(
            server_event(
                ServerEventType.TURN_COMPLETE,
                turn_id=turn.turn_id,
                metrics={"ttfa_ms": 1.0},
            )
        )

    async def cancel(self) -> None:
        self.cancel_hook_started.set()
        await asyncio.sleep(5)

    async def close(self) -> None:
        self.closed = True


class _MathTranscriberSession:
    async def push_audio(self, chunk: bytes) -> StreamingTranscript | None:
        del chunk
        return None

    async def finish_turn(self) -> StreamingTranscript:
        return StreamingTranscript("Please calculate that", is_final=True)

    async def cancel(self) -> None:
        return None

    async def close(self) -> None:
        return None


class _MathTranscriber:
    async def start_session(self, audio_format: PcmAudioFormat):
        del audio_format
        return _MathTranscriberSession()


class _ScriptedMathLanguageModel:
    def __init__(self, responses: tuple[tuple[str, ...], ...]) -> None:
        self._responses = responses
        self._index = 0

    async def stream(self, transcript: str, cancel_event: threading.Event):
        del transcript
        deltas = self._responses[self._index]
        self._index += 1
        for delta in deltas:
            if cancel_event.is_set():
                return
            yield delta


class _TinySynthesizer:
    async def warmup(self) -> None:
        return None

    async def start_turn(self, turn_id: str) -> None:
        del turn_id

    async def synthesize_chunk(
        self, text: str, cancel_event: threading.Event
    ) -> SynthesizedAudioChunk:
        del text, cancel_event
        return SynthesizedAudioChunk(b"\x01\x00", 24_000, 1, synthesis_ms=1.0)

    async def cancel(self) -> None:
        return None

    async def close(self) -> None:
        return None


class RealtimeServerTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="realtime_api_", dir=SERVICE_ROOT)
        self.addCleanup(temporary.cleanup)
        self.runtime_root = Path(temporary.name)

    def _app(
        self,
        *,
        registry: ReadinessRegistry | None = None,
        processor=None,
        inference_available: bool = True,
        warmup: WarmupCoordinator | None = None,
        readiness_probe=None,
        capacity_snapshot=None,
    ):
        selected_processor = processor or _ImmediateProcessor()
        return (
            api_module.create_app(
                service=_Service(self.runtime_root / "incoming"),
                shutdown_callback=None,
                readiness_registry=registry,
                warmup_coordinator=warmup,
                realtime_processor_factory=lambda: selected_processor,
                realtime_inference_available=inference_available,
                realtime_readiness_probe=readiness_probe,
                realtime_capacity_snapshot=capacity_snapshot,
            ),
            selected_processor,
        )

    def test_liveness_and_readiness_are_separate_without_automatic_warmup(self) -> None:
        registry = ReadinessRegistry(api_module.READINESS_COMPONENTS)
        application, _processor = self._app(registry=registry)

        with TestClient(application) as client:
            health = client.get("/health")
            readiness = client.get("/ready")

        self.assertEqual(
            {"status": "ok", "service": "aira-local-voice-api"}, health.json()
        )
        self.assertEqual(200, health.status_code)
        self.assertEqual(503, readiness.status_code)
        self.assertEqual("starting", readiness.json()["status"])
        self.assertEqual("no-store", readiness.headers["cache-control"])

    def test_live_tts_probe_degrades_ready_endpoint_and_handshake(self) -> None:
        def unavailable() -> None:
            raise RuntimeError("worker path must not leak")

        application, _processor = self._app(
            registry=_ready_registry(), readiness_probe=unavailable
        )
        with TestClient(application) as client:
            response = client.get("/ready")
            with client.websocket_connect("/v1/realtime") as websocket:
                websocket.send_json(_session_start())
                event = websocket.receive_json()
                self.assertEqual("degraded", event["readiness"]["status"])
                self.assertFalse(event["can_process_turns"])
                with self.assertRaises(WebSocketDisconnect) as closed:
                    websocket.receive_json()
                self.assertEqual(1013, closed.exception.code)

        self.assertEqual(503, response.status_code)
        self.assertEqual("degraded", response.json()["status"])
        self.assertEqual("degraded", response.json()["components"]["tts"])
        self.assertNotIn("path", response.json()["message"])

    def test_readiness_and_handshake_report_safe_queue_capacity(self) -> None:
        queues = {
            "stt": {"concurrency": 1, "active": 0, "queued": 2},
            "pocket_tts": {
                "busy": False,
                "queue_depth": 1,
                "queue_capacity": 8,
                "oldest_queue_wait_ms": 3.0,
            },
        }
        application, _processor = self._app(
            registry=_ready_registry(), capacity_snapshot=lambda: queues
        )

        with TestClient(application) as client:
            response = client.get("/ready")
            with client.websocket_connect("/v1/realtime") as websocket:
                websocket.send_json(_session_start())
                handshake = websocket.receive_json()
                websocket.send_json(_control("session_end"))

        self.assertEqual(200, response.status_code)
        self.assertEqual(queues, response.json()["queues"])
        self.assertEqual(queues, handshake["readiness"]["queues"])

    def test_degraded_and_failed_readiness_remain_unavailable_and_safe(self) -> None:
        cases = ("degraded", "failed")
        for status in cases:
            with self.subTest(status=status):
                registry = ReadinessRegistry(api_module.READINESS_COMPONENTS)
                if status == "degraded":
                    registry.mark_degraded(
                        "failure at /mnt/e/aira-local-runtime/private.gguf"
                    )
                else:
                    registry.finish_failed("tts")
                application, _processor = self._app(registry=registry)

                with TestClient(application) as client:
                    response = client.get("/ready")

                self.assertEqual(503, response.status_code)
                self.assertEqual(status, response.json()["status"])
                self.assertEqual("no-store", response.headers["cache-control"])
                self.assertNotIn("/mnt/", response.text)
                self.assertNotIn("\\", response.text)

    def test_controlled_warmup_runs_off_loop_and_keeps_health_live(self) -> None:
        registry = ReadinessRegistry(api_module.READINESS_COMPONENTS)
        entered = threading.Event()
        release = threading.Event()

        def first_step() -> None:
            entered.set()
            release.wait(timeout=2)

        coordinator = WarmupCoordinator(
            registry,
            (
                WarmupStep("stt", first_step),
                WarmupStep("llm", lambda: None),
                WarmupStep("tts", lambda: None),
            ),
        )
        application, _processor = self._app(
            registry=registry,
            warmup=coordinator,
        )

        try:
            with TestClient(application) as client:
                self.assertTrue(entered.wait(timeout=1))
                self.assertEqual(200, client.get("/health").status_code)
                self.assertEqual("warming", client.get("/ready").json()["status"])
                release.set()
                deadline = time.monotonic() + 1
                response = client.get("/ready")
                while response.status_code != 200 and time.monotonic() < deadline:
                    time.sleep(0.01)
                    response = client.get("/ready")
                self.assertEqual(200, response.status_code)
                self.assertEqual("ready", response.json()["status"])
        finally:
            release.set()

    def test_ready_session_processes_audio_and_can_accept_a_second_turn(self) -> None:
        application, processor = self._app(registry=_ready_registry())

        with TestClient(application) as client:
            with client.websocket_connect("/v1/realtime") as websocket:
                websocket.send_json(_session_start())
                ready = websocket.receive_json()
                self.assertEqual("session_ready", ready["type"])
                self.assertEqual("ready", ready["readiness"]["status"])
                self.assertTrue(ready["can_process_turns"])
                self.assertIn("AI", ready["ai_disclosure"])

                for _turn_index in range(2):
                    websocket.send_bytes(_meaningful_pcm())
                    websocket.send_json(_control("end_of_turn"))
                    events = [websocket.receive_json() for _ in range(3)]
                    audio_header = websocket.receive_json()
                    audio = websocket.receive_bytes()
                    complete = websocket.receive_json()
                    self.assertEqual(
                        ["stt_final", "thinking", "speaking", "audio_chunk"],
                        [event["type"] for event in [*events, audio_header]],
                    )
                    self.assertEqual(0, audio_header["sequence"])
                    self.assertEqual(24_000, audio_header["sample_rate_hz"])
                    self.assertEqual(b"\x01\x00\x02\x00", audio)
                    self.assertEqual("turn_complete", complete["type"])
                websocket.send_json(_control("session_end"))

        self.assertEqual(2, len(processor.turn_ids))
        self.assertTrue(processor.closed)

    def test_persistent_websocket_handles_twenty_mixed_math_turns(self) -> None:
        base_responses = (
            ("2 + 2",),
            ("sqrt(2)",),
            ("√2", "\n", "≈", " ", "1.41421356"),
            ("Pi is approximately 3.14159.",),
            ("10 / 3",),
            ("2^10 = 1024",),
            ("-2.75",),
            ("10.25%",),
            ("Pi is approximately 3.14159. It is irrational.",),
            ("**Calculation:**", "\n", "10 ÷ 4 = 2.5"),
        )
        responses = base_responses * 2
        expected_text = tuple("".join(parts) for parts in responses)
        processor = StreamingRealtimeTurnProcessor(
            _MathTranscriber(),
            _ScriptedMathLanguageModel(responses),
            _TinySynthesizer(),
        )
        application, _processor = self._app(
            registry=_ready_registry(), processor=processor
        )

        with TestClient(application) as client:
            with client.websocket_connect("/v1/realtime") as websocket:
                websocket.send_json(_session_start())
                websocket.receive_json()
                for expected in expected_text:
                    websocket.send_bytes(_meaningful_pcm())
                    websocket.send_json(_control("end_of_turn"))
                    deltas: list[str] = []
                    sentence_sequences: list[int] = []
                    audio_sequences: list[int] = []
                    while True:
                        event = websocket.receive_json()
                        if event["type"] == "text_delta":
                            self.assertTrue(event["delta"].strip())
                            deltas.append(event["delta"])
                        elif event["type"] == "text_sentence":
                            self.assertTrue(event["text"].strip())
                            sentence_sequences.append(event["sequence"])
                        elif event["type"] == "audio_chunk":
                            audio_sequences.append(event["sequence"])
                            self.assertEqual(event["byte_length"], len(websocket.receive_bytes()))
                        elif event["type"] == "turn_complete":
                            break
                    self.assertEqual(expected, "".join(deltas))
                    self.assertEqual(
                        list(range(1, len(sentence_sequences) + 1)),
                        sentence_sequences,
                    )
                    self.assertEqual(
                        list(range(len(audio_sequences))), audio_sequences
                    )
                websocket.send_json(_control("session_end"))

    def test_no_speech_is_recoverable_on_the_same_session(self) -> None:
        application, processor = self._app(registry=_ready_registry())

        with TestClient(application) as client:
            with client.websocket_connect("/v1/realtime") as websocket:
                websocket.send_json(_session_start())
                websocket.receive_json()
                websocket.send_bytes(b"\x00\x00" * 4_000)
                websocket.send_json(_control("end_of_turn"))
                error = websocket.receive_json()
                self.assertEqual("recoverable_error", error["type"])
                self.assertEqual("no_speech", error["code"])
                self.assertNotIn("metrics", error)

                websocket.send_bytes(_meaningful_pcm())
                websocket.send_json(_control("end_of_turn"))
                while websocket.receive_json()["type"] != "audio_chunk":
                    pass
                websocket.receive_bytes()
                self.assertEqual("turn_complete", websocket.receive_json()["type"])
                websocket.send_json(_control("session_end"))

        self.assertEqual(1, len(processor.turn_ids))

    def test_malformed_duplicate_and_oversized_messages_are_typed_and_recoverable(
        self,
    ) -> None:
        application, processor = self._app(registry=_ready_registry())

        with TestClient(application) as client:
            with client.websocket_connect("/v1/realtime") as websocket:
                websocket.send_json(_session_start())
                websocket.receive_json()

                websocket.send_text("not-json")
                malformed = websocket.receive_json()
                self.assertEqual("recoverable_error", malformed["type"])
                self.assertEqual("invalid_json", malformed["code"])

                websocket.send_json(_session_start())
                duplicate = websocket.receive_json()
                self.assertEqual("recoverable_error", duplicate["type"])
                self.assertEqual("duplicate_session_start", duplicate["code"])

                websocket.send_text(" " * (16 * 1024 + 1))
                oversized_control = websocket.receive_json()
                self.assertEqual("message_too_large", oversized_control["code"])

                websocket.send_bytes(b"\x00" * (64 * 1024 + 2))
                oversized_audio = websocket.receive_json()
                self.assertEqual("audio_chunk_too_large", oversized_audio["code"])

                websocket.send_bytes(_meaningful_pcm())
                websocket.send_json(_control("end_of_turn"))
                while websocket.receive_json()["type"] != "audio_chunk":
                    pass
                websocket.receive_bytes()
                self.assertEqual("turn_complete", websocket.receive_json()["type"])
                websocket.send_json(_control("session_end"))

        self.assertEqual(1, len(processor.turn_ids))

    def test_duplicate_end_of_turn_does_not_start_a_second_processor_task(self) -> None:
        processor = _BlockingProcessor()
        application, _processor = self._app(
            registry=_ready_registry(), processor=processor
        )

        with TestClient(application) as client:
            with client.websocket_connect("/v1/realtime") as websocket:
                websocket.send_json(_session_start())
                websocket.receive_json()
                websocket.send_bytes(_meaningful_pcm())
                websocket.send_json(_control("end_of_turn"))
                self.assertTrue(processor.started.wait(timeout=1))

                websocket.send_json(_control("end_of_turn"))
                duplicate = websocket.receive_json()
                self.assertEqual("recoverable_error", duplicate["type"])
                self.assertEqual("invalid_transition", duplicate["code"])

                websocket.send_json(_control("cancel_turn"))
                self.assertEqual("turn_cancelled", websocket.receive_json()["code"])
                websocket.send_json(_control("session_end"))

        self.assertTrue(processor.cancelled)
        self.assertTrue(processor.closed)

    def test_cancel_releases_processor_resources(self) -> None:
        processor = _BlockingProcessor()
        application, _processor = self._app(
            registry=_ready_registry(), processor=processor
        )

        with TestClient(application) as client:
            with client.websocket_connect("/v1/realtime") as websocket:
                websocket.send_json(_session_start())
                websocket.receive_json()
                websocket.send_bytes(_meaningful_pcm())
                websocket.send_json(_control("end_of_turn"))
                self.assertTrue(processor.started.wait(timeout=1))
                websocket.send_json(_control("cancel_turn"))
                cancelled = websocket.receive_json()
                self.assertEqual("turn_cancelled", cancelled["code"])
                self.assertNotIn("metrics", cancelled)
                self.assertTrue(processor.finished.wait(timeout=1))
                websocket.send_json(_control("session_end"))

        self.assertTrue(processor.cancelled)
        self.assertTrue(processor.closed)

    def test_cancel_ack_is_bounded_and_suppresses_all_late_turn_output(self) -> None:
        processor = _CancellationRaceProcessor()
        application, _processor = self._app(
            registry=_ready_registry(), processor=processor
        )

        with TestClient(application) as client:
            with client.websocket_connect("/v1/realtime") as websocket:
                websocket.send_json(_session_start())
                websocket.receive_json()
                websocket.send_bytes(_meaningful_pcm())
                websocket.send_json(_control("end_of_turn"))
                self.assertTrue(processor.started.wait(timeout=1))

                started = time.monotonic()
                websocket.send_json(_control("cancel_turn"))
                acknowledgement = websocket.receive_json()
                self.assertLess(time.monotonic() - started, 1.0)
                self.assertEqual("turn_cancelled", acknowledgement["code"])
                self.assertTrue(processor.cancel_hook_started.wait(timeout=1))

                # The same socket must accept a clean turn. Its first event is
                # proof that no stale text/audio/completion escaped turn one.
                websocket.send_bytes(_meaningful_pcm())
                websocket.send_json(_control("end_of_turn"))
                second_stt = websocket.receive_json()
                self.assertEqual("stt_final", second_stt["type"])
                second_complete = websocket.receive_json()
                self.assertEqual("turn_complete", second_complete["type"])
                self.assertNotEqual(
                    acknowledgement.get("turn_id"), second_complete["turn_id"]
                )
                websocket.send_json(_control("session_end"))

        self.assertEqual(2, processor.calls)
        self.assertTrue(processor.closed)

    def test_abrupt_disconnect_cancels_turn_and_closes_processor(self) -> None:
        processor = _BlockingProcessor()
        application, _processor = self._app(
            registry=_ready_registry(), processor=processor
        )

        with TestClient(application) as client:
            with client.websocket_connect("/v1/realtime") as websocket:
                websocket.send_json(_session_start())
                websocket.receive_json()
                websocket.send_bytes(_meaningful_pcm())
                websocket.send_json(_control("end_of_turn"))
                self.assertTrue(processor.started.wait(timeout=1))
                # Exiting the WebSocket context simulates a peer disappearing
                # without cancel_turn or session_end.

        self.assertTrue(processor.finished.wait(timeout=1))
        self.assertTrue(processor.cancelled)
        self.assertTrue(processor.closed)

    def test_not_ready_and_invalid_handshakes_close_with_typed_events(self) -> None:
        starting = ReadinessRegistry(api_module.READINESS_COMPONENTS)
        application, _processor = self._app(registry=starting)
        with TestClient(application) as client:
            with client.websocket_connect("/v1/realtime") as websocket:
                websocket.send_json(_session_start())
                event = websocket.receive_json()
                self.assertEqual("session_ready", event["type"])
                self.assertFalse(event["can_process_turns"])
                with self.assertRaises(WebSocketDisconnect) as closed:
                    websocket.receive_json()
                self.assertEqual(1013, closed.exception.code)

            with client.websocket_connect("/v1/realtime") as websocket:
                websocket.send_json(_session_start(version=99))
                event = websocket.receive_json()
                self.assertEqual("fatal_error", event["type"])
                self.assertEqual("unsupported_protocol_version", event["code"])
                with self.assertRaises(WebSocketDisconnect) as closed:
                    websocket.receive_json()
                self.assertEqual(1008, closed.exception.code)

            with client.websocket_connect("/v1/realtime") as websocket:
                websocket.send_json(_session_start(companion="tara"))
                event = websocket.receive_json()
                self.assertEqual("fatal_error", event["type"])
                self.assertEqual("unsupported_companion", event["code"])
                with self.assertRaises(WebSocketDisconnect) as closed:
                    websocket.receive_json()
                self.assertEqual(1008, closed.exception.code)

    def test_warmup_environment_is_explicit_opt_in(self) -> None:
        self.assertFalse(api_module._model_warmup_enabled({}))
        self.assertFalse(
            api_module._model_warmup_enabled(
                {api_module.MODEL_WARMUP_ENVIRONMENT: "disabled"}
            )
        )
        for value in ("1", "true", "YES", "on"):
            with self.subTest(value=value):
                self.assertTrue(
                    api_module._model_warmup_enabled(
                        {api_module.MODEL_WARMUP_ENVIRONMENT: value}
                    )
                )


if __name__ == "__main__":
    unittest.main()
