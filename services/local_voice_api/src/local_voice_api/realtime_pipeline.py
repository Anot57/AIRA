"""Composable streaming turn pipeline used by the realtime transport."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Protocol

from .conversation import normalize_companion_transcript
from .observability import (
    RealtimeConversationMonitor,
    current_realtime_debug_turn,
)
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
    RealtimeSttTimeoutError,
    RealtimeSynthesizer,
    SpeakableTextChunker,
    StreamingRealtimeSynthesizer,
    StreamingLanguageModel,
    StreamingTranscriber,
)

_LOGGER = logging.getLogger(__name__)
MAX_ACCEPTED_NO_SPEECH_PROBABILITY = 0.60


class RealtimeEventSink(Protocol):
    async def send_json(self, event: dict[str, object]) -> None: ...

    async def send_audio(self, audio: bytes) -> None: ...


class RealtimeTurnProcessor(Protocol):
    async def prepare(self) -> None: ...

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

    async def prepare(self) -> None:
        return None

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
        *,
        debug_conversation: bool = False,
        stt_timeout_seconds: float = 30.0,
    ) -> None:
        if stt_timeout_seconds <= 0:
            raise ValueError("Realtime STT timeout must be positive.")
        self._transcriber = transcriber
        self._language_model = language_model
        self._synthesizer = synthesizer
        self._active_transcriber = None
        self._conversation_monitor = RealtimeConversationMonitor(
            debug_conversation,
            logger=_LOGGER,
        )
        self._stt_timeout_seconds = stt_timeout_seconds

    async def prepare(self) -> None:
        prepare = getattr(self._language_model, "prepare", None)
        if prepare is not None:
            await prepare()

    async def process_turn(
        self,
        turn: RealtimeTurn,
        sink: RealtimeEventSink,
        cancel_event: threading.Event,
    ) -> None:
        with self._conversation_monitor.turn(
            session_id=turn.session_id,
            turn_id=turn.turn_id,
            generation=turn.generation,
            metrics=turn.metrics,
        ) as debug_turn:
            try:
                await self._process_turn(turn, sink, cancel_event)
            except asyncio.CancelledError:
                if debug_turn is not None:
                    debug_turn.cancelled()
                raise
            except Exception:
                if debug_turn is not None:
                    debug_turn.failed()
                raise

    async def _process_turn(
        self,
        turn: RealtimeTurn,
        sink: RealtimeEventSink,
        cancel_event: threading.Event,
    ) -> None:
        stt_terminal_status: str | None = None

        def mark_stt_terminal(status: str) -> None:
            nonlocal stt_terminal_status
            if stt_terminal_status is not None:
                return
            stt_terminal_status = status
            self._log_timing(turn, "stt_terminal", status=status)

        _raise_if_cancelled(cancel_event)
        self._log_timing(turn, "stt_session_start")
        try:
            transcriber_session = await self._transcriber.start_session(
                turn.audio_format
            )
        except asyncio.CancelledError:
            mark_stt_terminal("cancelled")
            raise
        except Exception:
            mark_stt_terminal("error")
            raise
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
            try:
                transcript = await asyncio.wait_for(
                    transcriber_session.finish_turn(),
                    timeout=self._stt_timeout_seconds,
                )
            except asyncio.CancelledError:
                mark_stt_terminal("cancelled")
                raise
            except TimeoutError as error:
                mark_stt_terminal("timeout")
                raise RealtimeSttTimeoutError(
                    "Realtime transcription exceeded its bounded deadline."
                ) from error
            except NoSpeechDetectedError:
                mark_stt_terminal("no_speech")
                raise
            except Exception:
                mark_stt_terminal("error")
                raise
            if transcript.stt_queue_enter_us is not None:
                turn.metrics.mark_boundary(
                    "stt_queue_enter",
                    monotonic_us=transcript.stt_queue_enter_us,
                )
            if transcript.stt_start_us is not None:
                turn.metrics.mark_boundary(
                    "stt_start", monotonic_us=transcript.stt_start_us
                )
            if transcript.stt_complete_us is not None:
                turn.metrics.mark_boundary(
                    "stt_complete", monotonic_us=transcript.stt_complete_us
                )
            else:
                turn.metrics.mark_boundary("stt_complete")
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
                mark_stt_terminal("error")
                raise RuntimeError("Realtime transcription did not return a final result.")
            if not isinstance(transcript.text, str) or not transcript.text.strip():
                mark_stt_terminal("no_speech")
                raise NoSpeechDetectedError("No speech was detected in this turn.")
            if (
                transcript.stt_no_speech_probability is not None
                and transcript.stt_no_speech_probability
                >= MAX_ACCEPTED_NO_SPEECH_PROBABILITY
            ):
                mark_stt_terminal("no_speech")
                raise NoSpeechDetectedError(
                    "Whisper classified this turn as likely non-speech."
                )
            normalized = normalize_companion_transcript("aanya", transcript.text)
            if len(normalized) > MAX_TRANSCRIPT_TEXT_CHARACTERS:
                mark_stt_terminal("error")
                raise RuntimeError("Realtime transcript exceeded its safe limit.")
            mark_stt_terminal("stt_complete")
            debug_turn = current_realtime_debug_turn()
            if debug_turn is not None:
                debug_turn.log_user(normalized)
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
                debug_turn = current_realtime_debug_turn()
                if debug_turn is not None:
                    debug_turn.add_response_text(delta)
                if first_meaningful_text:
                    turn.metrics.mark_boundary("first_meaningful_text")
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
            turn.metrics.mark_boundary("llm_request_start")
            stream_turn = getattr(self._language_model, "stream_turn", None)
            if stream_turn is None:
                response_stream = self._language_model.stream(
                    normalized, cancel_event
                )
            else:
                response_stream = stream_turn(
                    normalized,
                    cancel_event,
                    session_id=turn.session_id,
                    generation=turn.generation,
                )
            async for delta in response_stream:
                _raise_if_cancelled(cancel_event)
                if not isinstance(delta, str):
                    raise RuntimeError("Realtime LLM deltas must be text.")
                if not delta:
                    continue
                response_characters += len(delta)
                if response_characters > MAX_RESPONSE_TEXT_CHARACTERS:
                    raise RuntimeError("Realtime response text exceeded its safe limit.")
                if first_delta:
                    turn.metrics.mark_boundary("llm_first_token")
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
            turn.metrics.mark_boundary("turn_complete")
            turn.metrics.mark_complete()
            self._log_timing(turn, "turn_complete")
            turn.metrics.log(_LOGGER)
            debug_turn = current_realtime_debug_turn()
            if debug_turn is not None:
                debug_turn.completed()
            await sink.send_json(
                server_event(
                    ServerEventType.TURN_COMPLETE,
                    turn_id=turn.turn_id,
                    metrics=turn.metrics.snapshot(),
                )
            )
        finally:
            if stt_terminal_status is None:
                mark_stt_terminal(
                    "cancelled" if cancel_event.is_set() else "error"
                )
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
            if (
                turn.metrics.boundary_monotonic_us("reaction_selected")
                is not None
            ):
                turn.metrics.mark_boundary("reaction_tts_start")
                self._log_timing(turn, "reaction_tts_start")
            self._log_timing(turn, "tts_queue_enter")
            turn.metrics.mark_boundary("tts_queue_enter")
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
                turn.metrics.mark_boundary("tts_first_pcm")
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
                    turn.metrics.mark_boundary("first_audio_binary_sent")
                    turn.metrics.mark_boundary("first_audio")
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
            try:
                cancel_language_model = getattr(
                    self._language_model, "cancel_active_turn", None
                )
                if cancel_language_model is not None:
                    await cancel_language_model()
            finally:
                await self._synthesizer.cancel()

    async def close(self) -> None:
        try:
            if self._active_transcriber is not None:
                await self._active_transcriber.close()
        finally:
            self._active_transcriber = None
            try:
                await self._synthesizer.close()
            finally:
                close_language_model = getattr(self._language_model, "close", None)
                if close_language_model is not None:
                    await close_language_model()


def _raise_if_cancelled(cancel_event: threading.Event) -> None:
    if cancel_event.is_set():
        raise asyncio.CancelledError
