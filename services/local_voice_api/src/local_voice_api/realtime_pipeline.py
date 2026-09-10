"""Composable streaming turn pipeline used by the realtime transport."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Protocol

from .conversation import normalize_companion_transcript
from .realtime_protocol import (
    MAX_AUDIO_CHUNK_BYTES,
    MAX_PLAYBACK_SAMPLE_RATE_HZ,
    MAX_RESPONSE_TEXT_CHARACTERS,
    MAX_TEXT_DELTA_CHARACTERS,
    MAX_TRANSCRIPT_TEXT_CHARACTERS,
    MIN_PLAYBACK_SAMPLE_RATE_HZ,
    ServerEventType,
    server_event,
)
from .realtime_session import RealtimeTurn
from .streaming import (
    LeadingSpeakerLabelNormalizer,
    NoSpeechDetectedError,
    RealtimeInferenceUnavailableError,
    RealtimeSynthesizer,
    SpeakableTextChunker,
    StreamingRealtimeSynthesizer,
    StreamingLanguageModel,
    StreamingTranscriber,
)

_LOGGER = logging.getLogger(__name__)


class RealtimeEventSink(Protocol):
    async def send_json(self, event: dict[str, object]) -> None: ...

    async def send_audio(self, audio: bytes) -> None: ...


class RealtimeTurnProcessor(Protocol):
    async def process_turn(
        self,
        turn: RealtimeTurn,
        sink: RealtimeEventSink,
        cancel_event: threading.Event,
    ) -> None: ...

    async def cancel(self) -> None: ...

    async def close(self) -> None: ...


class UnavailableRealtimeTurnProcessor:
    """Honest default until production realtime inference adapters are selected."""

    async def process_turn(
        self,
        turn: RealtimeTurn,
        sink: RealtimeEventSink,
        cancel_event: threading.Event,
    ) -> None:
        del turn, sink, cancel_event
        raise RealtimeInferenceUnavailableError(
            "Realtime inference adapters are not enabled on this local server."
        )

    async def cancel(self) -> None:
        return None

    async def close(self) -> None:
        return None


class StreamingRealtimeTurnProcessor:
    """Run STT -> streaming LLM -> chunk TTS without full-response blocking."""

    def __init__(
        self,
        transcriber: StreamingTranscriber,
        language_model: StreamingLanguageModel,
        synthesizer: RealtimeSynthesizer | StreamingRealtimeSynthesizer,
    ) -> None:
        self._transcriber = transcriber
        self._language_model = language_model
        self._synthesizer = synthesizer
        self._active_transcriber = None

    async def process_turn(
        self,
        turn: RealtimeTurn,
        sink: RealtimeEventSink,
        cancel_event: threading.Event,
    ) -> None:
        _raise_if_cancelled(cancel_event)
        self._log_timing(turn, "stt_session_start")
        transcriber_session = await self._transcriber.start_session(turn.audio_format)
        self._log_timing(turn, "stt_session_ready")
        self._active_transcriber = transcriber_session
        sentence_sequence = 0
        audio_sequence = 0
        first_delta = True
        first_sentence = True
        first_meaningful_text = True
        first_audio = True
        response_characters = 0
        chunker = SpeakableTextChunker(min_characters=12)
        prefix_normalizer = LeadingSpeakerLabelNormalizer()
        try:
            self._log_timing(
                turn,
                "stt_audio_push_start",
                input_chunks=turn.input_chunk_count,
                input_bytes=len(turn.audio),
            )
            for offset in range(0, len(turn.audio), MAX_AUDIO_CHUNK_BYTES):
                await transcriber_session.push_audio(
                    turn.audio[offset : offset + MAX_AUDIO_CHUNK_BYTES]
                )
                _raise_if_cancelled(cancel_event)
            self._log_timing(
                turn,
                "stt_queue_enter",
                input_chunks=turn.input_chunk_count,
                input_bytes=len(turn.audio),
            )
            transcript = await transcriber_session.finish_turn()
            self._log_timing(
                turn,
                "stt_start",
                monotonic_us=transcript.stt_start_us,
                queue_wait_ms=transcript.stt_queue_wait_ms,
            )
            stt_complete_fields: dict[str, object] = {
                "stt_duration_ms": transcript.stt_duration_ms,
                "segment_count": transcript.segment_count,
            }
            if transcript.stt_no_speech_probability is not None:
                stt_complete_fields["stt_no_speech_probability"] = (
                    transcript.stt_no_speech_probability
                )
            self._log_timing(
                turn,
                "stt_complete",
                monotonic_us=transcript.stt_complete_us,
                **stt_complete_fields,
            )
            _raise_if_cancelled(cancel_event)
            if transcript.is_final is not True:
                raise RuntimeError("Realtime transcription did not return a final result.")
            if not isinstance(transcript.text, str) or not transcript.text.strip():
                raise NoSpeechDetectedError("No speech was detected in this turn.")
            normalized = normalize_companion_transcript("aanya", transcript.text)
            if len(normalized) > MAX_TRANSCRIPT_TEXT_CHARACTERS:
                raise RuntimeError("Realtime transcript exceeded its safe limit.")
            turn.metrics.mark_stt_final()
            await sink.send_json(
                server_event(
                    ServerEventType.STT_FINAL,
                    turn_id=turn.turn_id,
                    text=normalized,
                )
            )
            await sink.send_json(
                server_event(ServerEventType.THINKING, turn_id=turn.turn_id)
            )
            self._log_timing(turn, "tts_turn_start")
            await self._synthesizer.start_turn(turn.turn_id)
            self._log_timing(turn, "tts_turn_ready")

            async def forward_normalized_text(delta: str) -> None:
                nonlocal audio_sequence, first_audio, first_sentence
                nonlocal sentence_sequence, first_meaningful_text
                if not delta:
                    return
                if first_meaningful_text:
                    self._log_timing(turn, "first_meaningful_text")
                    first_meaningful_text = False
                for offset in range(0, len(delta), MAX_TEXT_DELTA_CHARACTERS):
                    await sink.send_json(
                        server_event(
                            ServerEventType.TEXT_DELTA,
                            turn_id=turn.turn_id,
                            delta=delta[offset : offset + MAX_TEXT_DELTA_CHARACTERS],
                        )
                    )
                for sentence in chunker.feed(delta):
                    if first_sentence:
                        turn.metrics.mark_first_speakable_chunk()
                        self._log_timing(turn, "first_speakable_text")
                        first_sentence = False
                    sentence_sequence += 1
                    await sink.send_json(
                        server_event(
                            ServerEventType.TEXT_SENTENCE,
                            turn_id=turn.turn_id,
                            sequence=sentence_sequence,
                            text=sentence,
                        )
                    )
                    audio_sequence, first_audio = await self._synthesize_and_send(
                        turn,
                        sink,
                        cancel_event,
                        sentence,
                        audio_sequence,
                        first_audio,
                    )

            self._log_timing(turn, "llm_request_start")
            async for delta in self._language_model.stream(normalized, cancel_event):
                _raise_if_cancelled(cancel_event)
                if not isinstance(delta, str):
                    raise RuntimeError("Realtime LLM deltas must be text.")
                if not delta:
                    continue
                response_characters += len(delta)
                if response_characters > MAX_RESPONSE_TEXT_CHARACTERS:
                    raise RuntimeError("Realtime response text exceeded its safe limit.")
                if first_delta:
                    turn.metrics.mark_first_llm_token()
                    self._log_timing(turn, "llm_first_token")
                    first_delta = False
                await forward_normalized_text(prefix_normalizer.feed(delta))

            await forward_normalized_text(prefix_normalizer.finish())

            for sentence in chunker.finish():
                if first_sentence:
                    turn.metrics.mark_first_speakable_chunk()
                    self._log_timing(turn, "first_speakable_text")
                    first_sentence = False
                sentence_sequence += 1
                await sink.send_json(
                    server_event(
                        ServerEventType.TEXT_SENTENCE,
                        turn_id=turn.turn_id,
                        sequence=sentence_sequence,
                        text=sentence,
                    )
                )
                audio_sequence, first_audio = await self._synthesize_and_send(
                    turn,
                    sink,
                    cancel_event,
                    sentence,
                    audio_sequence,
                    first_audio,
                )
            if first_sentence or first_audio:
                raise RuntimeError("Realtime response contained no speakable audio.")
            _raise_if_cancelled(cancel_event)
            turn.metrics.mark_complete()
            self._log_timing(turn, "turn_complete")
            turn.metrics.log(_LOGGER)
            await sink.send_json(
                server_event(
                    ServerEventType.TURN_COMPLETE,
                    turn_id=turn.turn_id,
                    metrics=turn.metrics.snapshot(),
                )
            )
        finally:
            self._active_transcriber = None
            await transcriber_session.close()

    async def _synthesize_and_send(
        self,
        turn: RealtimeTurn,
        sink: RealtimeEventSink,
        cancel_event: threading.Event,
        sentence: str,
        sequence: int,
        first_audio: bool,
    ) -> tuple[int, bool]:
        _raise_if_cancelled(cancel_event)
        if first_audio:
            self._log_timing(turn, "tts_queue_enter")
        synthesis_started = time.perf_counter()
        previous_chunk_at = synthesis_started
        yielded_audio = False
        async for audio in self._synthesis_chunks(sentence, cancel_event):
            chunk_ready_at = time.perf_counter()
            measured_synthesis_ms = round(
                max(0.0, chunk_ready_at - previous_chunk_at) * 1000.0, 3
            )
            previous_chunk_at = chunk_ready_at
            _raise_if_cancelled(cancel_event)
            if not audio.pcm:
                raise RuntimeError("Realtime synthesizer returned empty audio.")
            if (
                audio.channels != 1
                or not MIN_PLAYBACK_SAMPLE_RATE_HZ
                <= audio.sample_rate_hz
                <= MAX_PLAYBACK_SAMPLE_RATE_HZ
            ):
                raise RuntimeError("Realtime synthesizer returned unsupported PCM audio.")
            if first_audio:
                turn.metrics.mark_first_audio_sample()
                self._log_timing(
                    turn,
                    "tts_first_pcm",
                    sample_rate_hz=audio.sample_rate_hz,
                    channels=audio.channels,
                )
                await sink.send_json(
                    server_event(ServerEventType.SPEAKING, turn_id=turn.turn_id)
                )
            synthesis_ms = (
                audio.synthesis_ms
                if audio.synthesis_ms is not None
                else measured_synthesis_ms
            )
            duration = audio.audio_duration_seconds
            realtime_factor = (
                synthesis_ms / (duration * 1000.0)
                if duration > 0
                else None
            )
            frame_width = audio.channels * audio.sample_width_bytes
            frame_limit = MAX_AUDIO_CHUNK_BYTES - (MAX_AUDIO_CHUNK_BYTES % frame_width)
            for offset in range(0, len(audio.pcm), frame_limit):
                _raise_if_cancelled(cancel_event)
                pcm_frame = audio.pcm[offset : offset + frame_limit]
                frame_duration = len(pcm_frame) / (
                    audio.sample_rate_hz
                    * audio.channels
                    * audio.sample_width_bytes
                )
                await sink.send_json(
                    server_event(
                        ServerEventType.AUDIO_CHUNK,
                        turn_id=turn.turn_id,
                        sequence=sequence,
                        encoding="pcm_s16le",
                        sample_rate_hz=audio.sample_rate_hz,
                        channels=audio.channels,
                        byte_length=len(pcm_frame),
                        audio_duration_ms=round(frame_duration * 1000.0, 3),
                        synthesis_ms=synthesis_ms,
                        realtime_factor=(
                            round(realtime_factor, 4)
                            if realtime_factor is not None
                            else None
                        ),
                    )
                )
                await sink.send_audio(pcm_frame)
                if first_audio:
                    turn.metrics.mark_first_audio_sent()
                    self._log_timing(
                        turn,
                        "first_audio_binary_sent",
                        byte_length=len(pcm_frame),
                    )
                    first_audio = False
                sequence += 1
            yielded_audio = True
        if not yielded_audio:
            raise RuntimeError("Realtime synthesizer returned no audio chunks.")
        return sequence, first_audio

    @staticmethod
    def _log_timing(
        turn: RealtimeTurn,
        event: str,
        **fields: object,
    ) -> None:
        turn.metrics.log_event(
            _LOGGER,
            event,
            session_id=turn.session_id,
            generation=turn.generation,
            **fields,
        )

    async def _synthesis_chunks(
        self, sentence: str, cancel_event: threading.Event
    ):
        if isinstance(self._synthesizer, StreamingRealtimeSynthesizer):
            async for audio in self._synthesizer.synthesize_stream(
                sentence, cancel_event
            ):
                yield audio
            return
        audio = await self._synthesizer.synthesize_chunk(sentence, cancel_event)
        yield audio

    async def cancel(self) -> None:
        try:
            if self._active_transcriber is not None:
                await self._active_transcriber.cancel()
        finally:
            await self._synthesizer.cancel()

    async def close(self) -> None:
        try:
            if self._active_transcriber is not None:
                await self._active_transcriber.close()
        finally:
            self._active_transcriber = None
            await self._synthesizer.close()


def _raise_if_cancelled(cancel_event: threading.Event) -> None:
    if cancel_event.is_set():
        raise asyncio.CancelledError
