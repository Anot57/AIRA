"""Deterministic checks for streaming adapters, chunking, and TTFA events."""

from __future__ import annotations

import asyncio
import logging
import struct
import sys
import threading
import unittest
from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = SERVICE_ROOT / "src"
sys.path.insert(0, str(SOURCE_ROOT))

from local_voice_api.realtime_pipeline import (  # noqa: E402
    StreamingRealtimeTurnProcessor,
)
from local_voice_api.realtime_protocol import (  # noqa: E402
    MAX_AUDIO_CHUNK_BYTES,
    PcmAudioFormat,
)
from local_voice_api.realtime_session import (  # noqa: E402
    MIN_SPEECH_AUDIO_BYTES,
    RealtimeSession,
)
from local_voice_api.streaming import (  # noqa: E402
    BoundedBatchStreamingTranscriber,
    FinalTranscriptionResult,
    LeadingSpeakerLabelNormalizer,
    NoSpeechDetectedError,
    SpeakableTextChunker,
    StreamingTranscript,
    SynthesizedAudioChunk,
    SttAdmissionController,
)


def _turn():
    session = RealtimeSession("session_pipeline")
    session.start("aanya", PcmAudioFormat())
    session.begin_listening()
    session.push_audio(
        struct.pack("<h", 1000) * (MIN_SPEECH_AUDIO_BYTES // 2)
    )
    return session.finish_audio()


class _Sink:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []
        self.audio: list[bytes] = []
        self.first_audio_sent = asyncio.Event()

    async def send_json(self, event: dict[str, object]) -> None:
        self.events.append(event)

    async def send_audio(self, audio: bytes) -> None:
        self.audio.append(audio)
        self.first_audio_sent.set()


class _TranscriberSession:
    def __init__(self) -> None:
        self.audio = bytearray()
        self.cancelled = False
        self.closed = False

    async def push_audio(self, chunk: bytes) -> StreamingTranscript | None:
        self.audio.extend(chunk)
        return None

    async def finish_turn(self) -> StreamingTranscript:
        return StreamingTranscript("Hello Anna", is_final=True)

    async def cancel(self) -> None:
        self.cancelled = True

    async def close(self) -> None:
        self.closed = True


class _Transcriber:
    def __init__(self, session: _TranscriberSession | None = None) -> None:
        self.session = session or _TranscriberSession()

    async def start_session(self, audio_format: PcmAudioFormat):
        self.audio_format = audio_format
        return self.session


class _LanguageModel:
    async def stream(
        self, transcript: str, cancel_event: threading.Event
    ):
        self.transcript = transcript
        for delta in ("Hello there.", " I am an AI companion."):
            if cancel_event.is_set():
                return
            yield delta


class _MutableLanguageModel:
    def __init__(self) -> None:
        self.deltas: tuple[str, ...] = ()

    async def stream(
        self, transcript: str, cancel_event: threading.Event
    ):
        del transcript
        for delta in self.deltas:
            if cancel_event.is_set():
                return
            yield delta


class _FreshTranscriber:
    async def start_session(self, audio_format: PcmAudioFormat):
        del audio_format
        return _TranscriberSession()


class _Synthesizer:
    def __init__(self) -> None:
        self.started_turn: str | None = None
        self.texts: list[str] = []
        self.cancelled = False
        self.closed = False

    async def warmup(self) -> None:
        return None

    async def start_turn(self, turn_id: str) -> None:
        self.started_turn = turn_id

    async def synthesize_chunk(
        self, text: str, cancel_event: threading.Event
    ) -> SynthesizedAudioChunk:
        self.texts.append(text)
        # Force transport framing while retaining a realistic 24 kHz Qwen rate.
        return SynthesizedAudioChunk(
            pcm=b"\x01\x00" * 35_000,
            sample_rate_hz=24_000,
            channels=1,
            synthesis_ms=25.0,
        )

    async def cancel(self) -> None:
        self.cancelled = True

    async def close(self) -> None:
        self.closed = True


class _SmallSynthesizer(_Synthesizer):
    async def synthesize_chunk(
        self, text: str, cancel_event: threading.Event
    ) -> SynthesizedAudioChunk:
        del cancel_event
        self.texts.append(text)
        return SynthesizedAudioChunk(b"\x01\x00", 24_000, 1, synthesis_ms=1.0)


class _StreamingSynthesizer:
    def __init__(self) -> None:
        self.started_turn: str | None = None

    async def warmup(self) -> None:
        return None

    async def start_turn(self, turn_id: str) -> None:
        self.started_turn = turn_id

    async def synthesize_stream(
        self, text: str, cancel_event: threading.Event
    ):
        del text
        for pcm in (b"\x01\x00", b"\x02\x00"):
            if cancel_event.is_set():
                return
            yield SynthesizedAudioChunk(pcm, 24_000, 1, synthesis_ms=2.0)

    async def cancel(self) -> None:
        return None

    async def close(self) -> None:
        return None


class _GatedStreamingSynthesizer(_StreamingSynthesizer):
    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()

    async def synthesize_stream(
        self, text: str, cancel_event: threading.Event
    ):
        del text
        yield SynthesizedAudioChunk(b"\x01\x00", 24_000, 1)
        await self.release.wait()
        if not cancel_event.is_set():
            yield SynthesizedAudioChunk(b"\x02\x00", 24_000, 1)


class StreamingPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_latency_boundaries_are_correlated_and_content_free(self) -> None:
        processor = StreamingRealtimeTurnProcessor(
            _Transcriber(), _LanguageModel(), _SmallSynthesizer()
        )
        turn = _turn()
        sink = _Sink()

        with self.assertLogs(
            "local_voice_api.realtime_pipeline", level=logging.INFO
        ) as captured:
            await processor.process_turn(turn, sink, turn.cancel_event)

        logs = "\n".join(captured.output)
        for event in (
            "stt_queue_enter",
            "stt_start",
            "stt_complete",
            "llm_request_start",
            "llm_first_token",
            "first_meaningful_text",
            "tts_queue_enter",
            "tts_first_pcm",
            "first_audio_binary_sent",
        ):
            self.assertIn(f"event={event}", logs)
        self.assertIn("session_id=session_pipeline", logs)
        self.assertIn("generation=1", logs)
        self.assertRegex(logs, r"monotonic_us=\d+")
        self.assertNotIn("Hello Anna", logs)
        self.assertNotIn("Hello there", logs)
        self.assertNotIn("AI companion", logs)

    async def test_twenty_sequential_turns_share_one_normalized_text_stream(
        self,
    ) -> None:
        language_model = _MutableLanguageModel()
        synthesizer = _SmallSynthesizer()
        processor = StreamingRealtimeTurnProcessor(
            _FreshTranscriber(), language_model, synthesizer
        )

        for index in range(20):
            expected = f"Response {index}."
            language_model.deltas = (
                ("Aanya", ": ", expected)
                if index % 2 == 0
                else ("Res", f"ponse {index}.")
            )
            turn = _turn()
            sink = _Sink()

            await processor.process_turn(turn, sink, turn.cancel_event)

            displayed = "".join(
                str(event["delta"])
                for event in sink.events
                if event["type"] == "text_delta"
            )
            sentences = [
                event
                for event in sink.events
                if event["type"] == "text_sentence"
            ]
            self.assertEqual(expected, displayed)
            self.assertEqual([expected], [event["text"] for event in sentences])
            self.assertEqual([1], [event["sequence"] for event in sentences])
            self.assertNotEqual("", sentences[0]["text"])
            self.assertEqual(expected, synthesizer.texts[-1])

    async def test_first_binary_audio_is_sent_before_stream_completion(self) -> None:
        synthesizer = _GatedStreamingSynthesizer()
        processor = StreamingRealtimeTurnProcessor(
            _Transcriber(), _LanguageModel(), synthesizer
        )
        turn = _turn()
        sink = _Sink()

        processing = asyncio.create_task(
            processor.process_turn(turn, sink, turn.cancel_event)
        )
        await asyncio.wait_for(sink.first_audio_sent.wait(), timeout=1)

        self.assertEqual([b"\x01\x00"], sink.audio)
        self.assertFalse(processing.done())
        synthesizer.release.set()
        await processing

    async def test_pipeline_forwards_incremental_synthesizer_chunks_in_order(
        self,
    ) -> None:
        synthesizer = _StreamingSynthesizer()
        processor = StreamingRealtimeTurnProcessor(
            _Transcriber(), _LanguageModel(), synthesizer
        )
        turn = _turn()
        sink = _Sink()

        await processor.process_turn(turn, sink, turn.cancel_event)

        self.assertEqual(turn.turn_id, synthesizer.started_turn)
        self.assertEqual(b"\x01\x00", sink.audio[0])
        self.assertEqual(b"\x02\x00", sink.audio[1])
        audio_headers = [
            event for event in sink.events if event["type"] == "audio_chunk"
        ]
        self.assertEqual(list(range(len(audio_headers))), [
            event["sequence"] for event in audio_headers
        ])
        self.assertEqual(24_000, audio_headers[0]["sample_rate_hz"])

    async def test_pipeline_streams_bounded_audio_and_reports_real_boundaries(
        self,
    ) -> None:
        transcriber = _Transcriber()
        language_model = _LanguageModel()
        synthesizer = _Synthesizer()
        processor = StreamingRealtimeTurnProcessor(
            transcriber, language_model, synthesizer
        )
        turn = _turn()
        sink = _Sink()

        await processor.process_turn(turn, sink, turn.cancel_event)

        event_types = [event["type"] for event in sink.events]
        self.assertEqual("stt_final", event_types[0])
        self.assertIn("thinking", event_types)
        self.assertIn("text_delta", event_types)
        self.assertIn("text_sentence", event_types)
        self.assertIn("speaking", event_types)
        self.assertEqual("turn_complete", event_types[-1])
        self.assertEqual("Hello Aanya", language_model.transcript)
        self.assertEqual(turn.turn_id, synthesizer.started_turn)
        self.assertTrue(transcriber.session.closed)

        audio_headers = [
            event for event in sink.events if event["type"] == "audio_chunk"
        ]
        self.assertGreaterEqual(len(audio_headers), 2)
        self.assertEqual(
            list(range(len(audio_headers))),
            [event["sequence"] for event in audio_headers],
        )
        self.assertTrue(
            all(event["byte_length"] <= MAX_AUDIO_CHUNK_BYTES for event in audio_headers)
        )
        self.assertEqual(
            [event["byte_length"] for event in audio_headers],
            [len(audio) for audio in sink.audio],
        )
        self.assertEqual(24_000, audio_headers[0]["sample_rate_hz"])
        self.assertTrue(
            all(header["audio_duration_ms"] > 0 for header in audio_headers)
        )
        self.assertTrue(
            all(header["synthesis_ms"] == 25.0 for header in audio_headers)
        )
        self.assertTrue(
            all(header["realtime_factor"] > 0 for header in audio_headers)
        )
        sentence_headers = [
            event for event in sink.events if event["type"] == "text_sentence"
        ]
        self.assertEqual(
            list(range(1, len(sentence_headers) + 1)),
            [event["sequence"] for event in sentence_headers],
        )

        metrics = sink.events[-1]["metrics"]
        self.assertIsInstance(metrics, dict)
        for key in (
            "stt_final_ms",
            "ttft_ms",
            "ttfs_ms",
            "ttfas_ms",
            "ttfa_ms",
            "total_ms",
        ):
            self.assertIn(key, metrics)
        self.assertLessEqual(metrics["ttfas_ms"], metrics["ttfa_ms"])
        self.assertLessEqual(metrics["ttfa_ms"], metrics["total_ms"])

    async def test_processor_cancel_reaches_active_adapters(self) -> None:
        transcriber = _Transcriber()
        synthesizer = _Synthesizer()
        processor = StreamingRealtimeTurnProcessor(
            transcriber, _LanguageModel(), synthesizer
        )
        processor._active_transcriber = transcriber.session

        await processor.cancel()
        await processor.close()

        self.assertTrue(transcriber.session.cancelled)
        self.assertTrue(synthesizer.cancelled)
        self.assertTrue(synthesizer.closed)

    async def test_pre_cancelled_turn_starts_no_inference_or_fake_metrics(self) -> None:
        transcriber = _Transcriber()
        synthesizer = _Synthesizer()
        processor = StreamingRealtimeTurnProcessor(
            transcriber, _LanguageModel(), synthesizer
        )
        turn = _turn()
        turn.cancel_event.set()
        sink = _Sink()

        with self.assertRaises(asyncio.CancelledError):
            await processor.process_turn(turn, sink, turn.cancel_event)

        self.assertFalse(hasattr(transcriber, "audio_format"))
        self.assertIsNone(synthesizer.started_turn)
        self.assertEqual([], sink.events)
        self.assertEqual({}, turn.metrics.snapshot())

    async def test_adapter_cleanup_continues_when_transcriber_hooks_fail(self) -> None:
        class FailingCleanupSession(_TranscriberSession):
            async def cancel(self) -> None:
                raise RuntimeError("cancel hook failed")

            async def close(self) -> None:
                raise RuntimeError("close hook failed")

        synthesizer = _Synthesizer()
        processor = StreamingRealtimeTurnProcessor(
            _Transcriber(), _LanguageModel(), synthesizer
        )
        processor._active_transcriber = FailingCleanupSession()

        with self.assertRaisesRegex(RuntimeError, "cancel hook failed"):
            await processor.cancel()
        self.assertTrue(synthesizer.cancelled)

        with self.assertRaisesRegex(RuntimeError, "close hook failed"):
            await processor.close()
        self.assertTrue(synthesizer.closed)
        self.assertIsNone(processor._active_transcriber)

    async def test_oversized_transcript_fails_before_any_text_is_sent(self) -> None:
        class OversizedTranscriptSession(_TranscriberSession):
            async def finish_turn(self) -> StreamingTranscript:
                return StreamingTranscript("x" * 10_001, is_final=True)

        synthesizer = _Synthesizer()
        processor = StreamingRealtimeTurnProcessor(
            _Transcriber(OversizedTranscriptSession()),
            _LanguageModel(),
            synthesizer,
        )
        turn = _turn()
        sink = _Sink()

        with self.assertRaisesRegex(RuntimeError, "transcript exceeded"):
            await processor.process_turn(turn, sink, turn.cancel_event)

        self.assertEqual([], sink.events)
        self.assertTrue(processor._active_transcriber is None)
        self.assertTrue(processor._transcriber.session.closed)
        self.assertIsNone(synthesizer.started_turn)


class StreamingAdapterTests(unittest.IsolatedAsyncioTestCase):
    def test_leading_speaker_label_normalizer_handles_stream_boundaries(self) -> None:
        cases = (
            (("Aanya", ": ", "I'm here."), "I'm here."),
            (("Aan", "ya: ", "I'm here."), "I'm here."),
            ((" AANYA: I'm here.",), "I'm here."),
            (("Aanya - I'm here.",), "I'm here."),
            (("Aanya — I'm here.",), "I'm here."),
            (("Assistant: I'm here.",), "I'm here."),
            (("My name ", "is Aanya."), "My name is Aanya."),
            (("Aanya ", "is a beautiful name."), "Aanya is a beautiful name."),
            (("I am Aanya.",), "I am Aanya."),
            (("You can call me Aanya.",), "You can call me Aanya."),
            (("I think Aanya is a nice name.",), "I think Aanya is a nice name."),
            (("Aanya: My name is Aanya.",), "My name is Aanya."),
            (("Assistant: Yes, I'm an AI companion.",), "Yes, I'm an AI companion."),
            (("√2", "\n", "≈", " ", "1.41421356"), "√2\n≈ 1.41421356"),
        )

        for deltas, expected in cases:
            with self.subTest(deltas=deltas):
                normalizer = LeadingSpeakerLabelNormalizer()
                output = "".join(normalizer.feed(delta) for delta in deltas)
                output += normalizer.finish()
                self.assertEqual(expected, output)

    def test_chunker_does_not_split_inside_decimal_values(self) -> None:
        cases = (
            "√2 ≈ 1.41421356",
            "Pi is approximately 3.14159.",
            "10 / 3",
            "-2.75",
            "10.25%",
        )
        for text in cases:
            with self.subTest(text=text):
                chunker = SpeakableTextChunker(min_characters=1)
                emitted = (*chunker.feed(text), *chunker.finish())
                self.assertEqual((text,), emitted)

        chunker = SpeakableTextChunker(min_characters=1)
        text = "Pi is approximately 3.14159. It is irrational."
        emitted = (*chunker.feed(text), *chunker.finish())
        self.assertEqual(
            ("Pi is approximately 3.14159.", "It is irrational."),
            emitted,
        )

    async def test_stt_admission_is_bounded_and_waiting_cancel_is_prompt(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        calls = 0

        def blocked_transcription(_audio: bytes, _format: PcmAudioFormat) -> str:
            nonlocal calls
            calls += 1
            entered.set()
            release.wait(timeout=1)
            return "heard"

        admission = SttAdmissionController(concurrency=1)
        adapter = BoundedBatchStreamingTranscriber(
            blocked_transcription,
            admission=admission,
        )
        first = await adapter.start_session(PcmAudioFormat())
        second = await adapter.start_session(PcmAudioFormat())
        await first.push_audio(b"\x01\x00")
        await second.push_audio(b"\x01\x00")
        first_task = asyncio.create_task(first.finish_turn())
        self.assertTrue(await asyncio.to_thread(entered.wait, 1))
        second_task = asyncio.create_task(second.finish_turn())
        for _ in range(50):
            if admission.snapshot()["queued"] == 1:
                break
            await asyncio.sleep(0.005)
        self.assertEqual(1, admission.snapshot()["active"])
        self.assertEqual(1, admission.snapshot()["queued"])
        await second.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(second_task, timeout=0.2)
        release.set()
        self.assertEqual("heard", (await first_task).text)
        self.assertEqual(1, calls)
        self.assertEqual({"concurrency": 1, "active": 0, "queued": 0}, admission.snapshot())

    async def test_bounded_batch_adapter_finishes_once_and_reports_no_speech(
        self,
    ) -> None:
        adapter = BoundedBatchStreamingTranscriber(
            lambda audio, audio_format: "  " if audio == b"\x00\x00" else " hello ",
            max_audio_bytes=4,
        )
        empty = await adapter.start_session(PcmAudioFormat())
        await empty.push_audio(b"\x00\x00")
        with self.assertRaises(NoSpeechDetectedError):
            await empty.finish_turn()

        complete = await adapter.start_session(PcmAudioFormat())
        await complete.push_audio(b"\x01\x00")
        transcript = await complete.finish_turn()
        self.assertEqual("hello", transcript.text)
        self.assertIsNotNone(transcript.stt_queue_enter_us)
        self.assertIsNotNone(transcript.stt_start_us)
        self.assertIsNotNone(transcript.stt_complete_us)
        self.assertIsNotNone(transcript.stt_queue_wait_ms)
        self.assertIsNotNone(transcript.stt_duration_ms)
        with self.assertRaisesRegex(RuntimeError, "closed"):
            await complete.finish_turn()

        malformed = await adapter.start_session(PcmAudioFormat())
        with self.assertRaisesRegex(ValueError, "complete frames"):
            await malformed.push_audio(b"\x00")

        diagnostic = await BoundedBatchStreamingTranscriber(
            lambda _audio, _format: FinalTranscriptionResult(
                "Yes",
                segment_count=1,
                stt_no_speech_probability=0.2,
            )
        ).start_session(PcmAudioFormat())
        await diagnostic.push_audio(b"\x01\x00")
        diagnostic_transcript = await diagnostic.finish_turn()
        self.assertEqual(1, diagnostic_transcript.segment_count)
        self.assertEqual(
            0.2, diagnostic_transcript.stt_no_speech_probability
        )

    async def test_batch_transcriber_discards_a_result_after_cancellation(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        def blocked_transcription(_audio: bytes, _format: PcmAudioFormat) -> str:
            entered.set()
            release.wait(timeout=1)
            return "stale transcript"

        session = await BoundedBatchStreamingTranscriber(
            blocked_transcription
        ).start_session(PcmAudioFormat())
        await session.push_audio(b"\x01\x00")
        finishing = asyncio.create_task(session.finish_turn())
        self.assertTrue(await asyncio.to_thread(entered.wait, 1))
        await session.cancel()
        release.set()

        with self.assertRaises(asyncio.CancelledError):
            await finishing

    def test_chunker_emits_punctuation_at_delta_end_and_removes_directions(self) -> None:
        chunker = SpeakableTextChunker(min_characters=5, max_characters=30)

        self.assertEqual(("Hello there.",), chunker.feed("*smiles* Hello there."))
        self.assertEqual((), chunker.feed("Short tail"))
        self.assertEqual(("Short tail",), chunker.finish())

    def test_synthesized_audio_rejects_partial_or_non_pcm16_frames(self) -> None:
        with self.assertRaises(ValueError):
            SynthesizedAudioChunk(b"\x00", 16_000, 1)
        with self.assertRaises(ValueError):
            SynthesizedAudioChunk(b"\x00\x00", 16_000, 1, sample_width_bytes=1)
        for invalid_timing in (True, -1.0, float("inf"), float("nan")):
            with self.subTest(synthesis_ms=invalid_timing):
                with self.assertRaises(ValueError):
                    SynthesizedAudioChunk(
                        b"\x00\x00",
                        16_000,
                        1,
                        synthesis_ms=invalid_timing,
                    )


if __name__ == "__main__":
    unittest.main()
