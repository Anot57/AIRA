"""Replaceable streaming STT, LLM, and chunk-oriented TTS contracts."""

from __future__ import annotations

import asyncio
import logging
import math
import re
import threading
import time
import wave
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from .realtime_protocol import (
    MAX_AUDIO_CHUNK_BYTES,
    MAX_TURN_AUDIO_BYTES,
    PcmAudioFormat,
)

_STAGE_DIRECTION = re.compile(r"(?:\*[^*\n]+\*|\[[^\]\n]+\])")
_SENTENCE_BOUNDARY = re.compile(r"[.!?;:]\s|\n")
_LOGGER = logging.getLogger(__name__)


class LeadingSpeakerLabelNormalizer:
    """Canonicalize the first streamed LLM text without emitting blank deltas."""

    _LABELS = ("aanya", "assistant")
    _SEPARATORS = (":", "-", "—")
    _MAX_PREFIX_CHARACTERS = 32
    _MAX_PENDING_WHITESPACE = 32

    def __init__(self) -> None:
        self._buffer = ""
        self._decided = False
        self._pending_whitespace = ""

    def feed(self, delta: str) -> str:
        if not isinstance(delta, str):
            raise TypeError("LLM deltas must be strings.")
        if self._decided:
            return self._emit_meaningful(delta)
        self._buffer += delta
        return self._decide(final=False)

    def finish(self) -> str:
        if self._decided:
            self._pending_whitespace = ""
            return ""
        return self._decide(final=True)

    def _decide(self, *, final: bool) -> str:
        candidate = self._buffer.lstrip()
        leading_characters = len(self._buffer) - len(candidate)
        folded = candidate.casefold()

        if not candidate:
            if not final and len(self._buffer) < self._MAX_PREFIX_CHARACTERS:
                return ""
            return self._flush_unchanged()

        matching_label = next(
            (label for label in self._LABELS if folded.startswith(label)), None
        )
        if matching_label is not None:
            remainder = candidate[len(matching_label) :]
            punctuation = remainder.lstrip()
            if not punctuation:
                if not final and len(self._buffer) < self._MAX_PREFIX_CHARACTERS:
                    return ""
                return self._flush_unchanged()
            if punctuation[0] in self._SEPARATORS:
                normalized = punctuation[1:].lstrip()
                self._buffer = ""
                self._decided = True
                return self._emit_meaningful(normalized)
            return self._flush_unchanged()

        could_be_partial_label = any(label.startswith(folded) for label in self._LABELS)
        if (
            could_be_partial_label
            and not final
            and leading_characters + len(candidate) < self._MAX_PREFIX_CHARACTERS
        ):
            return ""
        return self._flush_unchanged()

    def _flush_unchanged(self) -> str:
        buffered = self._buffer
        self._buffer = ""
        self._decided = True
        return self._emit_meaningful(buffered)

    def _emit_meaningful(self, text: str) -> str:
        combined = f"{self._pending_whitespace}{text}"
        if not combined.strip():
            self._pending_whitespace = combined[-self._MAX_PENDING_WHITESPACE :]
            return ""
        self._pending_whitespace = ""
        return combined


class NoSpeechDetectedError(RuntimeError):
    """No useful speech was found in a completed audio turn."""


class RealtimeInferenceUnavailableError(RuntimeError):
    """The transport exists but a realtime inference adapter is unavailable."""


class RealtimeSttTimeoutError(RuntimeError):
    """A submitted realtime turn exceeded its bounded STT deadline."""


@dataclass(frozen=True, slots=True)
class StreamingTranscript:
    text: str
    is_final: bool
    stt_queue_enter_us: int | None = None
    stt_start_us: int | None = None
    stt_complete_us: int | None = None
    stt_queue_wait_ms: float | None = None
    stt_duration_ms: float | None = None
    segment_count: int | None = None
    stt_no_speech_probability: float | None = None


@dataclass(frozen=True, slots=True)
class FinalTranscriptionResult:
    """Content-free diagnostics returned by a batch STT implementation."""

    text: str
    segment_count: int | None = None
    stt_no_speech_probability: float | None = None


@dataclass(frozen=True, slots=True)
class SynthesizedAudioChunk:
    pcm: bytes
    sample_rate_hz: int
    channels: int
    sample_width_bytes: int = 2
    synthesis_ms: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.pcm, bytes):
            raise TypeError("Synthesized PCM must be bytes.")
        if type(self.sample_rate_hz) is not int or self.sample_rate_hz <= 0:
            raise ValueError("Synthesized audio sample rate must be positive.")
        if type(self.channels) is not int or self.channels <= 0:
            raise ValueError("Synthesized audio channel count must be positive.")
        if self.sample_width_bytes != 2:
            raise ValueError("Realtime synthesized audio must use PCM16 samples.")
        frame_width = self.channels * self.sample_width_bytes
        if len(self.pcm) % frame_width:
            raise ValueError("Synthesized PCM must contain complete audio frames.")
        if self.synthesis_ms is not None and (
            isinstance(self.synthesis_ms, bool)
            or not isinstance(self.synthesis_ms, (int, float))
            or not math.isfinite(float(self.synthesis_ms))
            or self.synthesis_ms < 0
        ):
            raise ValueError("Synthesis timing must be a finite non-negative number.")

    @property
    def audio_duration_seconds(self) -> float:
        denominator = self.sample_rate_hz * self.channels * self.sample_width_bytes
        return len(self.pcm) / denominator if denominator > 0 else 0.0


@runtime_checkable
class StreamingTranscriberSession(Protocol):
    async def push_audio(self, chunk: bytes) -> StreamingTranscript | None: ...

    async def finish_turn(self) -> StreamingTranscript: ...

    async def cancel(self) -> None: ...

    async def close(self) -> None: ...


@runtime_checkable
class StreamingTranscriber(Protocol):
    async def start_session(
        self, audio_format: PcmAudioFormat
    ) -> StreamingTranscriberSession: ...


@runtime_checkable
class StreamingLanguageModel(Protocol):
    def stream(
        self, transcript: str, cancel_event: threading.Event
    ) -> AsyncIterator[str]: ...


@runtime_checkable
class RealtimeSynthesizer(Protocol):
    async def warmup(self) -> None: ...

    async def start_turn(self, turn_id: str) -> None: ...

    async def synthesize_chunk(
        self, text: str, cancel_event: threading.Event
    ) -> SynthesizedAudioChunk: ...

    async def cancel(self) -> None: ...

    async def close(self) -> None: ...


@runtime_checkable
class StreamingRealtimeSynthesizer(Protocol):
    """Optional incremental-audio capability for a realtime synthesizer.

    The original ``RealtimeSynthesizer`` contract remains supported by the
    pipeline. Production adapters can implement this capability so audio is
    forwarded as it is generated instead of materializing a whole sentence.
    """

    async def warmup(self) -> None: ...

    async def start_turn(self, turn_id: str) -> None: ...

    def synthesize_stream(
        self, text: str, cancel_event: threading.Event
    ) -> AsyncIterator[SynthesizedAudioChunk]: ...

    async def cancel(self) -> None: ...

    async def close(self) -> None: ...


class BoundedBatchTranscriberSession:
    """Buffer bounded PCM and delegate only once at end-of-turn.

    This deliberately does not rerun batch Whisper for every packet. A true
    incremental transcriber can replace this implementation without changing
    the transport protocol.
    """

    def __init__(
        self,
        final_transcriber: Callable[
            [bytes, PcmAudioFormat], str | FinalTranscriptionResult
        ],
        audio_format: PcmAudioFormat,
        *,
        max_audio_bytes: int = MAX_TURN_AUDIO_BYTES,
        admission: SttAdmissionController | None = None,
    ) -> None:
        if type(max_audio_bytes) is not int or max_audio_bytes <= 0:
            raise ValueError("Streaming transcription buffer limit must be positive.")
        self._final_transcriber = final_transcriber
        self._audio_format = audio_format
        self._max_audio_bytes = max_audio_bytes
        self._admission = admission
        self._audio = bytearray()
        self._cancelled = False
        self._closed = False

    async def push_audio(self, chunk: bytes) -> StreamingTranscript | None:
        self._ensure_active()
        if not isinstance(chunk, bytes) or not chunk:
            raise ValueError("Streaming audio chunks must be non-empty bytes.")
        if len(chunk) > MAX_AUDIO_CHUNK_BYTES:
            raise ValueError("Streaming audio chunk limit exceeded.")
        if len(chunk) % (self._audio_format.channels * 2):
            raise ValueError("Streaming PCM chunks must contain complete frames.")
        if len(self._audio) + len(chunk) > self._max_audio_bytes:
            raise ValueError("Streaming transcription buffer limit exceeded.")
        self._audio.extend(chunk)
        return None

    async def finish_turn(self) -> StreamingTranscript:
        self._ensure_active()
        audio = bytes(self._audio)
        self._audio.clear()
        self._closed = True
        wait_started = time.perf_counter()
        queue_enter_us = time.perf_counter_ns() // 1_000
        if self._admission is not None:
            await self._admission.acquire(lambda: self._cancelled)
        stt_start_us = time.perf_counter_ns() // 1_000
        queue_wait_ms = (time.perf_counter() - wait_started) * 1000
        _LOGGER.info("Realtime STT admitted queue_wait_ms=%.3f", queue_wait_ms)
        try:
            stt_started = time.perf_counter()
            result = await asyncio.to_thread(
                self._final_transcriber, audio, self._audio_format
            )
            stt_complete_us = time.perf_counter_ns() // 1_000
            stt_duration_ms = (time.perf_counter() - stt_started) * 1000
        finally:
            if self._admission is not None:
                self._admission.release()
        if self._cancelled:
            raise asyncio.CancelledError
        if isinstance(result, FinalTranscriptionResult):
            text = result.text
            segment_count = result.segment_count
            no_speech_probability = result.stt_no_speech_probability
        else:
            text = result
            segment_count = None
            no_speech_probability = None
        normalized = text.strip() if isinstance(text, str) else ""
        if not normalized:
            raise NoSpeechDetectedError("No speech was detected in this turn.")
        return StreamingTranscript(
            text=normalized,
            is_final=True,
            stt_queue_enter_us=queue_enter_us,
            stt_start_us=stt_start_us,
            stt_complete_us=stt_complete_us,
            stt_queue_wait_ms=round(queue_wait_ms, 3),
            stt_duration_ms=round(stt_duration_ms, 3),
            segment_count=segment_count,
            stt_no_speech_probability=no_speech_probability,
        )

    async def cancel(self) -> None:
        self._audio.clear()
        self._cancelled = True

    async def close(self) -> None:
        self._audio.clear()
        self._closed = True

    def _ensure_active(self) -> None:
        if self._closed:
            raise RuntimeError("Streaming transcription session is closed.")
        if self._cancelled:
            raise asyncio.CancelledError


class BoundedBatchStreamingTranscriber:
    def __init__(
        self,
        final_transcriber: Callable[
            [bytes, PcmAudioFormat], str | FinalTranscriptionResult
        ],
        *,
        max_audio_bytes: int = MAX_TURN_AUDIO_BYTES,
        admission: SttAdmissionController | None = None,
    ) -> None:
        if type(max_audio_bytes) is not int or max_audio_bytes <= 0:
            raise ValueError("Streaming transcription buffer limit must be positive.")
        self._final_transcriber = final_transcriber
        self._max_audio_bytes = max_audio_bytes
        self._admission = admission

    async def start_session(
        self, audio_format: PcmAudioFormat
    ) -> BoundedBatchTranscriberSession:
        return BoundedBatchTranscriberSession(
            self._final_transcriber,
            audio_format,
            max_audio_bytes=self._max_audio_bytes,
            admission=self._admission,
        )


class SttAdmissionController:
    """Shared bounded STT admission with cancellation-aware queue waiting."""

    def __init__(self, concurrency: int = 1) -> None:
        if type(concurrency) is not int or concurrency <= 0 or concurrency > 16:
            raise ValueError("STT concurrency must be between 1 and 16.")
        self.concurrency = concurrency
        self._semaphore = asyncio.Semaphore(concurrency)
        self.active = 0
        self.waiting = 0

    async def acquire(self, cancelled: Callable[[], bool]) -> None:
        self.waiting += 1
        try:
            while True:
                if cancelled():
                    raise asyncio.CancelledError
                try:
                    await asyncio.wait_for(self._semaphore.acquire(), timeout=0.05)
                    self.active += 1
                    return
                except TimeoutError:
                    continue
        finally:
            self.waiting -= 1

    def release(self) -> None:
        if self.active <= 0:
            raise RuntimeError("STT admission release was unbalanced.")
        self.active -= 1
        self._semaphore.release()

    def snapshot(self) -> dict[str, int]:
        return {
            "concurrency": self.concurrency,
            "active": self.active,
            "queued": self.waiting,
        }


class CompleteResponseStreamingLlmAdapter:
    """Compatibility adapter; yields once until llama.cpp streaming is wired."""

    def __init__(self, generate: Callable[[str], str]) -> None:
        self._generate = generate

    async def stream(
        self, transcript: str, cancel_event: threading.Event
    ) -> AsyncIterator[str]:
        if cancel_event.is_set():
            return
        response = await asyncio.to_thread(self._generate, transcript)
        if cancel_event.is_set():
            return
        if not isinstance(response, str) or not response.strip():
            raise RuntimeError("The local language model returned no response.")
        yield response


class SpeakableTextChunker:
    """Incrementally emit bounded, punctuation-aware text for downstream TTS."""

    def __init__(self, *, min_characters: int = 24, max_characters: int = 180) -> None:
        if min_characters <= 0 or max_characters < min_characters:
            raise ValueError("Text chunk bounds are invalid.")
        self._minimum = min_characters
        self._maximum = max_characters
        self._buffer = ""

    def feed(self, delta: str) -> tuple[str, ...]:
        if not isinstance(delta, str):
            raise TypeError("LLM deltas must be strings.")
        self._buffer += delta
        return self._drain(final=False)

    def finish(self) -> tuple[str, ...]:
        return self._drain(final=True)

    def _drain(self, *, final: bool) -> tuple[str, ...]:
        chunks: list[str] = []
        while self._buffer:
            boundary = self._boundary()
            if boundary is None:
                if not final:
                    break
                boundary = len(self._buffer)
            raw, self._buffer = self._buffer[:boundary], self._buffer[boundary:]
            cleaned = _clean_speakable_text(raw)
            if cleaned:
                chunks.append(cleaned)
        return tuple(chunks)

    def _boundary(self) -> int | None:
        for match in _SENTENCE_BOUNDARY.finditer(self._buffer):
            boundary = match.end()
            if boundary >= self._minimum:
                return boundary
        if len(self._buffer) < self._maximum:
            if (
                len(self._buffer) >= self._minimum
                and self._buffer[-1:] in ".!?;:"
            ):
                return len(self._buffer)
            return None
        whitespace = self._buffer.rfind(" ", self._minimum, self._maximum + 1)
        return whitespace + 1 if whitespace >= self._minimum else self._maximum


def _clean_speakable_text(text: str) -> str:
    without_directions = _STAGE_DIRECTION.sub(" ", text)
    return " ".join(without_directions.split()).strip()


def read_pcm16_wav(path: Path) -> SynthesizedAudioChunk:
    """Read one PCM16 WAV into transport-ready samples using stdlib only."""

    with wave.open(str(path), "rb") as wav_file:
        channels = wav_file.getnchannels()
        sample_width = wav_file.getsampwidth()
        sample_rate = wav_file.getframerate()
        compression = wav_file.getcomptype()
        pcm = wav_file.readframes(wav_file.getnframes())
    if compression != "NONE" or sample_width != 2 or channels <= 0 or sample_rate <= 0:
        raise ValueError("Realtime TTS adapter requires an uncompressed PCM16 WAV.")
    return SynthesizedAudioChunk(
        pcm=pcm,
        sample_rate_hz=sample_rate,
        channels=channels,
        sample_width_bytes=sample_width,
    )
