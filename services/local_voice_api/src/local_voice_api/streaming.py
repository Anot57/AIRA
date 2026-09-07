"""Replaceable streaming STT, LLM, and chunk-oriented TTS contracts."""

from __future__ import annotations

import asyncio
import math
import re
import threading
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


class NoSpeechDetectedError(RuntimeError):
    """No useful speech was found in a completed audio turn."""


class RealtimeInferenceUnavailableError(RuntimeError):
    """The transport exists but a realtime inference adapter is unavailable."""


@dataclass(frozen=True, slots=True)
class StreamingTranscript:
    text: str
    is_final: bool


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
        final_transcriber: Callable[[bytes, PcmAudioFormat], str],
        audio_format: PcmAudioFormat,
        *,
        max_audio_bytes: int = MAX_TURN_AUDIO_BYTES,
    ) -> None:
        if type(max_audio_bytes) is not int or max_audio_bytes <= 0:
            raise ValueError("Streaming transcription buffer limit must be positive.")
        self._final_transcriber = final_transcriber
        self._audio_format = audio_format
        self._max_audio_bytes = max_audio_bytes
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
        text = await asyncio.to_thread(
            self._final_transcriber, audio, self._audio_format
        )
        if self._cancelled:
            raise asyncio.CancelledError
        normalized = text.strip() if isinstance(text, str) else ""
        if not normalized:
            raise NoSpeechDetectedError("No speech was detected in this turn.")
        return StreamingTranscript(text=normalized, is_final=True)

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
        final_transcriber: Callable[[bytes, PcmAudioFormat], str],
        *,
        max_audio_bytes: int = MAX_TURN_AUDIO_BYTES,
    ) -> None:
        if type(max_audio_bytes) is not int or max_audio_bytes <= 0:
            raise ValueError("Streaming transcription buffer limit must be positive.")
        self._final_transcriber = final_transcriber
        self._max_audio_bytes = max_audio_bytes

    async def start_session(
        self, audio_format: PcmAudioFormat
    ) -> BoundedBatchTranscriberSession:
        return BoundedBatchTranscriberSession(
            self._final_transcriber,
            audio_format,
            max_audio_bytes=self._max_audio_bytes,
        )


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
