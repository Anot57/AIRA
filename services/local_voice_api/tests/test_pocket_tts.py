"""Mock-only checks for the Pocket TTS worker boundary and production selection."""

from __future__ import annotations

import asyncio
import io
import struct
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SERVICE_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = SERVICE_ROOT / "src"
sys.path.insert(0, str(SOURCE_ROOT))

import local_voice_api.api as api_module  # noqa: E402
import local_voice_api.pocket_tts_worker as worker_module  # noqa: E402
from local_voice_api.pocket_tts import (  # noqa: E402
    POCKET_TTS_PROVIDER_ENVIRONMENT,
    PocketTtsWorkerConfig,
    PocketTtsWorkerSynthesizer,
)
from local_voice_api.realtime_pipeline import (  # noqa: E402
    StreamingRealtimeTurnProcessor,
)
from local_voice_api.streaming import (  # noqa: E402
    RealtimeInferenceUnavailableError,
)


class _FakeConnection:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakeResponse:
    status = 200

    def __init__(self, frames: list[bytes]) -> None:
        wire = b"".join(struct.pack("!I", len(frame)) + frame for frame in frames)
        self._body = io.BytesIO(wire + struct.pack("!I", 0))
        self.closed = False

    def getheader(self, name: str) -> str | None:
        return {
            "X-Aira-Pcm-Encoding": "pcm_s16le",
            "X-Aira-Sample-Rate": "24000",
            "X-Aira-Channels": "1",
        }.get(name)

    def read(self, size: int = -1) -> bytes:
        return self._body.read(size)

    def close(self) -> None:
        self.closed = True


class _FakeWorkerSynthesizer(PocketTtsWorkerSynthesizer):
    def __init__(self, frames: list[bytes]) -> None:
        super().__init__(PocketTtsWorkerConfig())
        self.connection = _FakeConnection()
        self.response = _FakeResponse(frames)
        self.cancel_payloads: list[dict[str, object] | None] = []

    def _open_stream(self, body: bytes, generation: int):
        del body, generation
        with self._state_lock:
            self._active_connection = self.connection
        return self.connection, self.response

    def _json_request(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
        timeout: float | None = None,
    ) -> dict[str, object]:
        del method, path, timeout
        self.cancel_payloads.append(payload)
        return {"cancelled": True}


class PocketTtsAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_adapter_and_worker_stream_over_loopback_with_fake_model(self) -> None:
        runtime = worker_module.PocketTtsRuntime(Path("voice"), Path("cache"))
        runtime._status = "ready"
        runtime._model = _FakeModel()
        runtime._voice_state = object()
        server = worker_module.PocketTtsWorkerServer(("127.0.0.1", 0), runtime)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        port = server.server_address[1]
        synthesizer = PocketTtsWorkerSynthesizer(
            PocketTtsWorkerConfig(worker_url=f"http://127.0.0.1:{port}")
        )
        try:
            with mock.patch.object(
                worker_module,
                "_tensor_to_pcm16",
                side_effect=(b"\x01\x00", b"\x02\x00"),
            ):
                synthesizer.check_ready()
                await synthesizer.start_turn("turn_loopback")
                chunks = [
                    chunk
                    async for chunk in synthesizer.synthesize_stream(
                        "Hello.", threading.Event()
                    )
                ]
            self.assertEqual([b"\x01\x00", b"\x02\x00"], [c.pcm for c in chunks])
            self.assertTrue(all(c.sample_rate_hz == 24_000 for c in chunks))
        finally:
            await synthesizer.close()
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=1)

    async def test_streams_pcm_chunks_in_order_with_fixed_audio_metadata(self) -> None:
        synthesizer = _FakeWorkerSynthesizer(
            [b"\x01\x00\x02\x00", b"\x03\x00"]
        )
        await synthesizer.start_turn("turn_1")

        chunks = [
            chunk
            async for chunk in synthesizer.synthesize_stream(
                "Hello from Aanya.", threading.Event()
            )
        ]

        self.assertEqual(
            [b"\x01\x00\x02\x00", b"\x03\x00"],
            [chunk.pcm for chunk in chunks],
        )
        self.assertTrue(all(chunk.sample_rate_hz == 24_000 for chunk in chunks))
        self.assertTrue(all(chunk.channels == 1 for chunk in chunks))
        self.assertTrue(all(chunk.synthesis_ms is not None for chunk in chunks))
        self.assertTrue(synthesizer.connection.closed)
        self.assertTrue(synthesizer.response.closed)

    async def test_cancel_invalidates_turn_and_suppresses_stale_chunks(self) -> None:
        synthesizer = _FakeWorkerSynthesizer([b"\x01\x00", b"\x02\x00"])
        await synthesizer.start_turn("turn_cancel")
        stream = synthesizer.synthesize_stream("Keep talking.", threading.Event())
        first = await anext(stream)
        self.assertEqual(b"\x01\x00", first.pcm)

        await synthesizer.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await anext(stream)
        self.assertTrue(synthesizer.connection.closed)
        self.assertEqual({"turn_id": "turn_cancel"}, synthesizer.cancel_payloads[-1])

    async def test_request_payload_is_bounded_before_worker_call(self) -> None:
        synthesizer = _FakeWorkerSynthesizer([b"\x01\x00"])
        await synthesizer.start_turn("turn_large")
        with self.assertRaisesRegex(ValueError, "request is too large"):
            async for _chunk in synthesizer.synthesize_stream(
                "x" * 5000, threading.Event()
            ):
                pass

    def test_worker_unavailable_and_model_not_ready_are_actionable(self) -> None:
        synthesizer = PocketTtsWorkerSynthesizer(PocketTtsWorkerConfig())
        with mock.patch.object(
            synthesizer,
            "_json_request",
            side_effect=RealtimeInferenceUnavailableError("offline"),
        ):
            with self.assertRaisesRegex(
                RealtimeInferenceUnavailableError, "offline"
            ):
                synthesizer.check_ready()

        with mock.patch.object(
            synthesizer,
            "_json_request",
            return_value={
                "status": "loading",
                "message": "Model is loading.",
                "sample_rate_hz": 24_000,
                "channels": 1,
                "encoding": "pcm_s16le",
            },
        ):
            with self.assertRaisesRegex(
                RealtimeInferenceUnavailableError, "Model is loading"
            ):
                synthesizer.check_ready()

    def test_configuration_rejects_non_loopback_worker(self) -> None:
        with self.assertRaisesRegex(ValueError, "loopback"):
            PocketTtsWorkerConfig(worker_url="http://192.168.1.10:8766")


class _FakeTensor:
    pass


class _FakeModel:
    sample_rate = 24_000

    def __init__(self) -> None:
        self.calls: list[tuple[object, str, bool]] = []

    def get_state_for_audio_prompt(self, path: str) -> object:
        self.voice_path = path
        return object()

    def generate_audio_stream(self, state: object, text: str, *, copy_state: bool):
        self.calls.append((state, text, copy_state))
        yield _FakeTensor()
        yield _FakeTensor()


class PocketTtsWorkerTests(unittest.TestCase):
    def _ready_runtime(self) -> tuple[worker_module.PocketTtsRuntime, _FakeModel]:
        runtime = worker_module.PocketTtsRuntime(Path("voice"), Path("cache"))
        model = _FakeModel()
        runtime._status = "ready"
        runtime._model = model
        runtime._voice_state = object()
        return runtime, model

    def test_runtime_reuses_model_and_voice_and_has_no_generation_queue(self) -> None:
        runtime, model = self._ready_runtime()
        with mock.patch.object(
            worker_module, "_tensor_to_pcm16", return_value=b"\x01\x00"
        ):
            first = runtime.stream_pcm("turn_1", "a" * 32, "First sentence.")
            self.assertEqual(b"\x01\x00", next(first))
            second = runtime.stream_pcm("turn_2", "b" * 32, "Second sentence.")
            with self.assertRaises(worker_module.WorkerRequestError) as captured:
                next(second)
            self.assertEqual(409, captured.exception.status)
            self.assertTrue(runtime.cancel("turn_1"))
            with self.assertRaises(StopIteration):
                next(first)

        self.assertEqual(1, len(model.calls))
        self.assertTrue(model.calls[0][2])
        self.assertFalse(runtime.snapshot()["busy"])

    def test_runtime_splits_worker_pcm_into_bounded_even_frames(self) -> None:
        runtime, _model = self._ready_runtime()
        oversized = b"\x01\x00" * (worker_module.MAX_PCM_FRAME_BYTES // 2 + 10)
        with mock.patch.object(
            worker_module, "_tensor_to_pcm16", return_value=oversized
        ):
            frames = list(runtime.stream_pcm("turn_1", "c" * 32, "Hello."))

        self.assertEqual(oversized * 2, b"".join(frames))
        self.assertTrue(all(frame for frame in frames))
        self.assertTrue(
            all(len(frame) <= worker_module.MAX_PCM_FRAME_BYTES for frame in frames)
        )
        self.assertTrue(all(len(frame) % 2 == 0 for frame in frames))

    def test_initialize_loads_model_and_cached_voice_exactly_once(self) -> None:
        with tempfile.TemporaryDirectory(dir=SERVICE_ROOT) as temporary:
            root = Path(temporary)
            voice = root / "aanya_voice.safetensors"
            cache = root / "cache"
            voice.write_bytes(b"safe")
            cache.mkdir()
            model = _FakeModel()
            fake_package = SimpleNamespace(
                TTSModel=SimpleNamespace(load_model=mock.Mock(return_value=model))
            )
            runtime = worker_module.PocketTtsRuntime(voice, cache)
            with (
                mock.patch.dict(sys.modules, {"pocket_tts": fake_package}),
                mock.patch.object(
                    worker_module.importlib.metadata,
                    "version",
                    return_value="3.1.0",
                ),
            ):
                runtime.initialize()
                runtime.initialize()

            fake_package.TTSModel.load_model.assert_called_once_with()
            self.assertEqual(str(voice.resolve()), model.voice_path)
            self.assertEqual("ready", runtime.snapshot()["status"])
            self.assertTrue(runtime.snapshot()["voice_loaded"])


class ProductionSelectionTests(unittest.TestCase):
    def test_pocket_provider_selects_existing_streaming_pipeline(self) -> None:
        application = api_module._create_default_application(
            {
                POCKET_TTS_PROVIDER_ENVIRONMENT: "pocket_worker",
                "AIRA_MODEL_WARMUP": "0",
            }
        )

        self.assertTrue(application.state.realtime_inference_available)
        processor = application.state.realtime_processor_factory()
        self.assertIsInstance(processor, StreamingRealtimeTurnProcessor)
        self.assertIsInstance(processor._synthesizer, PocketTtsWorkerSynthesizer)
        self.assertIsNone(application.state.warmup_coordinator)

    def test_unconfigured_provider_preserves_unavailable_default(self) -> None:
        application = api_module._create_default_application({})
        self.assertFalse(application.state.realtime_inference_available)

    def test_pocket_warmup_uses_worker_readiness_callback(self) -> None:
        readiness_check = mock.Mock()
        with mock.patch.object(
            api_module,
            "build_pocket_tts_readiness_check",
            return_value=readiness_check,
        ):
            application = api_module._create_default_application(
                {
                    POCKET_TTS_PROVIDER_ENVIRONMENT: "pocket_worker",
                    "AIRA_MODEL_WARMUP": "1",
                }
            )

        coordinator = application.state.warmup_coordinator
        self.assertIsNotNone(coordinator)
        self.assertIs(readiness_check, coordinator._steps[-1].callback)


if __name__ == "__main__":
    unittest.main()
