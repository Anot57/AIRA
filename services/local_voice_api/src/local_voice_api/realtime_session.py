"""Bounded realtime session state independent of transport and model runtimes."""

from __future__ import annotations

import sys
import threading
import time
import uuid
from array import array
from dataclasses import dataclass
from enum import StrEnum

from .observability import RealtimeLatencyMetrics
from .realtime_protocol import (
    MAX_AUDIO_CHUNK_BYTES,
    MAX_TURN_AUDIO_BYTES,
    PcmAudioFormat,
    ProtocolError,
)

MIN_SPEECH_AUDIO_BYTES = 8_000  # 250 ms of PCM16 mono at 16 kHz.
MIN_MEAN_ABSOLUTE_AMPLITUDE = 12.0


class RealtimeSessionState(StrEnum):
    CONNECTING = "connecting"
    READY = "ready"
    LISTENING = "listening"
    FINALIZING_STT = "finalizing_stt"
    THINKING = "thinking"
    SPEAKING = "speaking"
    CANCELLED = "cancelled"
    CLOSED = "closed"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class RealtimeTurn:
    session_id: str
    generation: int
    turn_id: str
    audio: bytes
    audio_format: PcmAudioFormat
    metrics: RealtimeLatencyMetrics
    cancel_event: threading.Event
    input_chunk_count: int
    first_audio_received_at: float
    last_audio_received_at: float
    end_of_turn_received_at: float


class RealtimeSession:
    """Validate transitions and own exactly one bounded active-turn buffer."""

    def __init__(self, session_id: str | None = None) -> None:
        self.session_id = session_id or f"session_{uuid.uuid4().hex}"
        self.state = RealtimeSessionState.CONNECTING
        self.companion: str | None = None
        self.audio_format: PcmAudioFormat | None = None
        self._audio = bytearray()
        self._active_turn_id: str | None = None
        self._cancel_requested = threading.Event()
        self._input_chunk_count = 0
        self._first_audio_received_at: float | None = None
        self._last_audio_received_at: float | None = None
        self._turn_generation = 0

    @property
    def active_turn_id(self) -> str | None:
        return self._active_turn_id

    @property
    def buffered_audio_bytes(self) -> int:
        return len(self._audio)

    @property
    def cancel_requested(self) -> threading.Event:
        return self._cancel_requested

    @property
    def turn_generation(self) -> int:
        return self._turn_generation

    def start(self, companion: str, audio_format: PcmAudioFormat) -> None:
        self._require(RealtimeSessionState.CONNECTING)
        if companion != "aanya":
            raise ProtocolError(
                "unsupported_companion",
                "Realtime voice currently supports only companion=aanya.",
                fatal=True,
            )
        self.companion = companion
        self.audio_format = audio_format
        self.state = RealtimeSessionState.READY

    def begin_listening(self) -> None:
        self._require(RealtimeSessionState.READY)
        self._audio.clear()
        self._active_turn_id = None
        self._input_chunk_count = 0
        self._first_audio_received_at = None
        self._last_audio_received_at = None
        # Never reuse a cancellation event. A cancelled ``asyncio.to_thread``
        # call can continue briefly after its coroutine is cancelled; clearing
        # the old event for a new turn would let that stale worker resume work.
        self._cancel_requested = threading.Event()
        self.state = RealtimeSessionState.LISTENING

    def push_audio(self, chunk: bytes) -> None:
        self._require(RealtimeSessionState.LISTENING)
        if not isinstance(chunk, bytes) or not chunk:
            raise ProtocolError("invalid_audio_chunk", "Audio chunks must be non-empty bytes.")
        if len(chunk) > MAX_AUDIO_CHUNK_BYTES:
            raise ProtocolError(
                "audio_chunk_too_large",
                f"An audio chunk may contain at most {MAX_AUDIO_CHUNK_BYTES} bytes.",
            )
        if len(chunk) % 2:
            raise ProtocolError(
                "invalid_audio_chunk",
                "PCM signed 16-bit audio chunks must contain complete samples.",
            )
        if len(self._audio) + len(chunk) > MAX_TURN_AUDIO_BYTES:
            self._audio.clear()
            raise ProtocolError(
                "turn_audio_too_large",
                "The realtime turn exceeded the bounded audio buffer.",
            )
        self._audio.extend(chunk)
        now = time.monotonic()
        self._input_chunk_count += 1
        self._first_audio_received_at = self._first_audio_received_at or now
        self._last_audio_received_at = now

    def finish_audio(self) -> RealtimeTurn:
        self._require(RealtimeSessionState.LISTENING)
        # Start at accepted end_of_turn handling, before scanning the bounded
        # PCM buffer, so validation work is not excluded from TTFA.
        turn_id = f"aanya_rt_{uuid.uuid4().hex}"
        metrics = RealtimeLatencyMetrics(turn_id)
        metrics.start_end_of_turn()
        end_of_turn_received_at = time.monotonic()
        if not _contains_meaningful_audio(self._audio):
            self._audio.clear()
            raise ProtocolError(
                "no_speech",
                "No clear speech was detected. Please speak a little longer; listening will continue automatically.",
            )
        if self.audio_format is None:
            raise RuntimeError("Started realtime session has no audio format.")
        self._turn_generation += 1
        turn = RealtimeTurn(
            session_id=self.session_id,
            generation=self._turn_generation,
            turn_id=turn_id,
            audio=bytes(self._audio),
            audio_format=self.audio_format,
            metrics=metrics,
            cancel_event=self._cancel_requested,
            input_chunk_count=self._input_chunk_count,
            first_audio_received_at=self._first_audio_received_at
            or end_of_turn_received_at,
            last_audio_received_at=self._last_audio_received_at
            or end_of_turn_received_at,
            end_of_turn_received_at=end_of_turn_received_at,
        )
        self._audio.clear()
        self._active_turn_id = turn_id
        self.state = RealtimeSessionState.FINALIZING_STT
        return turn

    def mark_thinking(self) -> None:
        self._transition(
            {RealtimeSessionState.FINALIZING_STT}, RealtimeSessionState.THINKING
        )

    def mark_speaking(self) -> None:
        self._transition(
            {RealtimeSessionState.THINKING}, RealtimeSessionState.SPEAKING
        )

    def complete_turn(self) -> None:
        self._transition(
            {
                RealtimeSessionState.FINALIZING_STT,
                RealtimeSessionState.THINKING,
                RealtimeSessionState.SPEAKING,
            },
            RealtimeSessionState.READY,
        )
        self._active_turn_id = None
        self.begin_listening()

    def recover_turn(self) -> None:
        if self.state not in {
            RealtimeSessionState.LISTENING,
            RealtimeSessionState.FINALIZING_STT,
            RealtimeSessionState.THINKING,
            RealtimeSessionState.SPEAKING,
            RealtimeSessionState.CANCELLED,
            RealtimeSessionState.ERROR,
        }:
            raise self._invalid_transition()
        self._audio.clear()
        self._active_turn_id = None
        self.state = RealtimeSessionState.READY
        self.begin_listening()

    def cancel_turn(self) -> str | None:
        if self.state not in {
            RealtimeSessionState.LISTENING,
            RealtimeSessionState.FINALIZING_STT,
            RealtimeSessionState.THINKING,
            RealtimeSessionState.SPEAKING,
        }:
            raise self._invalid_transition()
        turn_id = self._active_turn_id
        self._cancel_requested.set()
        self._audio.clear()
        self.state = RealtimeSessionState.CANCELLED
        return turn_id

    def fail(self) -> None:
        if self.state is not RealtimeSessionState.CLOSED:
            self._audio.clear()
            self._cancel_requested.set()
            self.state = RealtimeSessionState.ERROR

    def close(self) -> None:
        self._audio.clear()
        self._active_turn_id = None
        self._cancel_requested.set()
        self.state = RealtimeSessionState.CLOSED

    def _require(self, expected: RealtimeSessionState) -> None:
        if self.state is not expected:
            raise self._invalid_transition()

    def _transition(
        self,
        allowed: set[RealtimeSessionState],
        target: RealtimeSessionState,
    ) -> None:
        if self.state not in allowed:
            raise self._invalid_transition()
        self.state = target

    def _invalid_transition(self) -> ProtocolError:
        return ProtocolError(
            "invalid_transition",
            f"That event is not valid while the realtime session is {self.state.value}.",
        )


def _contains_meaningful_audio(audio: bytes | bytearray) -> bool:
    if len(audio) < MIN_SPEECH_AUDIO_BYTES or len(audio) % 2:
        return False
    samples = array("h")
    samples.frombytes(audio)
    if sys.byteorder != "little":
        samples.byteswap()
    mean_amplitude = sum(abs(sample) for sample in samples) / len(samples)
    return mean_amplitude >= MIN_MEAN_ABSOLUTE_AMPLITUDE
