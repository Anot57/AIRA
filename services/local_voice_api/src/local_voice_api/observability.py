"""Dependency-light, monotonic latency instrumentation for local voice turns."""

from __future__ import annotations

import logging
import math
import os
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any

_LOGGER = logging.getLogger(__name__)
_CURRENT_TIMING: ContextVar[TurnTiming | None] = ContextVar(
    "aira_current_turn_timing", default=None
)
DEBUG_CONVERSATION_ENVIRONMENT = "AIRA_DEBUG_CONVERSATION"


def _milliseconds(seconds: float) -> float:
    return round(max(0.0, seconds) * 1000.0, 3)


def _safe_field(value: object) -> str:
    """Render one bounded log field without exposing path-like details."""

    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    rendered = str(value).replace("\n", " ").replace("\r", " ")
    if "/" in rendered or "\\" in rendered:
        return "[redacted-path]"
    return rendered[:120]


@dataclass(frozen=True, slots=True)
class StageTiming:
    """One completed monotonic stage measurement."""

    stage: str
    elapsed_ms: float
    fields: Mapping[str, object]


class TurnTiming:
    """Thread-safe timing recorder shared across one HTTP or realtime turn."""

    def __init__(
        self,
        turn_id: str,
        *,
        clock: Callable[[], float] = time.perf_counter,
        logger: logging.Logger = _LOGGER,
    ) -> None:
        if not isinstance(turn_id, str) or not turn_id:
            raise ValueError("turn_id must be a non-empty string")
        self.turn_id = turn_id
        self._clock = clock
        self._logger = logger
        self._started_at = clock()
        self._measurements: list[StageTiming] = []
        self._lock = threading.Lock()

    @property
    def elapsed_ms(self) -> float:
        return _milliseconds(self._clock() - self._started_at)

    @contextmanager
    def bind(self) -> Iterator[TurnTiming]:
        token: Token[TurnTiming | None] = _CURRENT_TIMING.set(self)
        try:
            yield self
        finally:
            _CURRENT_TIMING.reset(token)

    @contextmanager
    def stage(self, name: str, **fields: object) -> Iterator[None]:
        started_at = self._clock()
        try:
            yield
        finally:
            self.record(name, self._clock() - started_at, **fields)

    def milestone(self, name: str, **fields: object) -> StageTiming:
        return self.record(name, self._clock() - self._started_at, **fields)

    def record(
        self,
        name: str,
        elapsed_seconds: float,
        **fields: object,
    ) -> StageTiming:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Timing stage names must be non-empty strings.")
        if not isinstance(elapsed_seconds, (int, float)) or not math.isfinite(
            elapsed_seconds
        ):
            raise ValueError("Timing durations must be finite numbers.")
        measurement = StageTiming(
            stage=name,
            elapsed_ms=_milliseconds(float(elapsed_seconds)),
            fields=dict(fields),
        )
        with self._lock:
            self._measurements.append(measurement)
        suffix = "".join(
            f" {key}={_safe_field(value)}" for key, value in sorted(fields.items())
        )
        self._logger.info(
            "[AIRA TIMING] turn_id=%s stage=%s elapsed_ms=%.3f%s",
            self.turn_id,
            name,
            measurement.elapsed_ms,
            suffix,
        )
        return measurement

    def snapshot(self) -> tuple[StageTiming, ...]:
        with self._lock:
            return tuple(self._measurements)

    def durations_ms(self) -> dict[str, float]:
        with self._lock:
            return {
                measurement.stage: measurement.elapsed_ms
                for measurement in self._measurements
            }


def current_turn_timing() -> TurnTiming | None:
    """Return the recorder bound to this request/thread, if any."""

    return _CURRENT_TIMING.get()


class RealtimeLatencyMetrics:
    """Measure first-token/text/audio boundaries from server end-of-turn."""

    def __init__(
        self,
        turn_id: str,
        *,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.turn_id = turn_id
        self._clock = clock
        self._end_of_turn_at: float | None = None
        self._milestones: dict[str, float] = {}
        self._boundaries: dict[str, float] = {}

    def start_end_of_turn(self) -> None:
        if self._end_of_turn_at is not None:
            raise RuntimeError("End-of-turn timing has already started.")
        self._end_of_turn_at = self._clock()
        self._boundaries["end_of_turn_received"] = self._end_of_turn_at
        self._boundaries["speech_end"] = self._end_of_turn_at

    def mark_boundary(self, name: str, *, monotonic_us: int | None = None) -> None:
        """Record an exact same-process boundary once for stage calculations."""

        if not isinstance(name, str) or not name.strip():
            raise ValueError("Boundary names must be non-empty strings.")
        if monotonic_us is not None and (
            isinstance(monotonic_us, bool)
            or not isinstance(monotonic_us, int)
            or monotonic_us < 0
        ):
            raise ValueError("monotonic_us must be a non-negative integer")
        self._boundaries.setdefault(
            name,
            self._clock() if monotonic_us is None else monotonic_us / 1_000_000,
        )

    def mark_stt_final(self) -> None:
        self._mark_once("stt_final_ms")

    def mark_first_llm_token(self) -> None:
        self._mark_once("ttft_ms")

    def mark_first_speakable_chunk(self) -> None:
        self._mark_once("ttfs_ms")

    def mark_first_audio_sample(self) -> None:
        self._mark_once("ttfas_ms")

    def mark_first_audio_sent(self) -> None:
        """Record the first synthesized bytes successfully sent to the client."""

        self._mark_once("ttfa_ms")

    def mark_complete(self) -> None:
        self._mark_once("total_ms")

    def _mark_once(self, name: str) -> float:
        if self._end_of_turn_at is None:
            raise RuntimeError("End-of-turn timing has not started.")
        if name in self._milestones:
            return self._milestones[name]
        elapsed = _milliseconds(self._clock() - self._end_of_turn_at)
        self._milestones[name] = elapsed
        return elapsed

    def snapshot(self) -> dict[str, float]:
        return {**self._milestones, **self.stage_breakdown()}

    def boundary_monotonic_us(self, name: str) -> int | None:
        """Return an existing boundary without recording another clock read."""

        boundary = self._boundaries.get(name)
        return round(boundary * 1_000_000) if boundary is not None else None

    def stage_breakdown(self) -> dict[str, float]:
        pairs = {
            "end_of_turn_to_stt_start_ms": (
                "end_of_turn_received",
                "stt_start",
            ),
            "stt_ms": ("stt_start", "stt_complete"),
            "stt_to_llm_start_ms": ("stt_complete", "llm_request_start"),
            "llm_ttft_ms": ("llm_request_start", "llm_first_token"),
            "meaningful_text_ms": (
                "llm_request_start",
                "first_meaningful_text",
            ),
            "tts_to_first_pcm_ms": ("tts_queue_enter", "tts_first_pcm"),
            "first_pcm_to_binary_sent_ms": (
                "tts_first_pcm",
                "first_audio_binary_sent",
            ),
            "stt_to_reaction_selected_ms": (
                "stt_complete",
                "reaction_selected",
            ),
            "reaction_selected_to_tts_start_ms": (
                "reaction_selected",
                "reaction_tts_start",
            ),
            "reaction_tts_to_first_audio_ms": (
                "reaction_tts_start",
                "first_audio",
            ),
            "retrieval_ms": ("retrieval_start", "retrieval_complete"),
            "retrieval_to_grounded_llm_ms": (
                "retrieval_complete",
                "grounded_llm_start",
            ),
            "grounded_llm_ttft_ms": (
                "grounded_llm_start",
                "grounded_first_token",
            ),
        }
        measured: dict[str, float] = {}
        for label, (start, end) in pairs.items():
            if start in self._boundaries and end in self._boundaries:
                measured[label] = _milliseconds(
                    self._boundaries[end] - self._boundaries[start]
                )
        return measured

    def log_event(
        self,
        logger: logging.Logger,
        event: str,
        *,
        session_id: str,
        generation: int,
        monotonic_us: int | None = None,
        **fields: object,
    ) -> None:
        """Log one content-free, monotonic realtime boundary.

        The event shares the exact monotonic clock used by the aggregate TTFA
        metrics. Callers may add bounded operational counts or durations, but
        never transcript or response text.
        """

        if not isinstance(event, str) or not event.strip():
            raise ValueError("Realtime timing event names must be non-empty strings.")
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("session_id must be a non-empty string")
        if isinstance(generation, bool) or not isinstance(generation, int):
            raise ValueError("generation must be an integer")
        if generation < 1:
            raise ValueError("generation must be positive")
        if monotonic_us is not None and (
            isinstance(monotonic_us, bool)
            or not isinstance(monotonic_us, int)
            or monotonic_us < 0
        ):
            raise ValueError("monotonic_us must be a non-negative integer")
        suffix = "".join(
            f" {key}={_safe_field(value)}" for key, value in sorted(fields.items())
        )
        logger.info(
            "[AIRA REALTIME TIMING] event=%s session_id=%s turn_id=%s "
            "generation=%d monotonic_us=%d%s",
            _safe_field(event),
            _safe_field(session_id),
            _safe_field(self.turn_id),
            generation,
            (
                round(self._clock() * 1_000_000)
                if monotonic_us is None
                else monotonic_us
            ),
            suffix,
        )

    def log(self, logger: logging.Logger = _LOGGER) -> None:
        fields = " ".join(
            f"{key}={value:.3f}" for key, value in sorted(self._milestones.items())
        )
        logger.info("[AIRA TTFA] turn_id=%s %s", self.turn_id, fields)


def debug_conversation_enabled(
    environ: Mapping[str, str] | None = None,
) -> bool:
    source = os.environ if environ is None else environ
    return source.get(DEBUG_CONVERSATION_ENVIRONMENT, "").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }


class RealtimeConversationDebugTurn:
    """Opt-in local terminal view over one realtime turn's existing metrics."""

    def __init__(
        self,
        *,
        session_id: str,
        turn_id: str,
        generation: int,
        metrics: RealtimeLatencyMetrics,
        logger: logging.Logger,
    ) -> None:
        self.session_id = session_id
        self.turn_id = turn_id
        self.generation = generation
        self.metrics = metrics
        self._logger = logger
        self._response_parts: list[str] = []
        self._mode = "none"
        self._retrieval_status = "not_applicable"
        self._retrieval_ms: float | None = None
        self._retrieval_completed_us: int | None = None
        self._turn_status = "active"
        self._aanya_logged = False
        self._summary_logged = False

    def log_user(self, transcript: str) -> None:
        self._logger.info(
            "[AIRA CONVERSATION] session_id=%s turn_id=%s\nUSER: %s",
            _safe_field(self.session_id),
            _safe_field(self.turn_id),
            _conversation_line(transcript),
        )

    def add_response_text(self, delta: str) -> None:
        if delta:
            self._response_parts.append(delta)

    def set_retrieval_mode(self, mode: str) -> None:
        self._mode = _safe_mode(mode)
        if self._mode == "none":
            self._retrieval_status = "not_applicable"
        elif self._retrieval_status == "not_applicable":
            self._retrieval_status = "pending"

    def retrieval_started(self, mode: str) -> None:
        self.set_retrieval_mode(mode)
        self._retrieval_status = "pending"

    def retrieval_finished(
        self,
        *,
        status: str,
        elapsed_ms: float,
        completed_us: int,
    ) -> None:
        self._retrieval_status = _safe_status(status)
        self._retrieval_ms = round(max(0.0, elapsed_ms), 3)
        self._retrieval_completed_us = completed_us
        self._log_summary_if_ready()

    def completed(self) -> None:
        if self._turn_status != "active":
            return
        self._turn_status = "completed"
        self._log_aanya_once()
        self._log_summary_if_ready()

    def cancelled(self) -> None:
        self._finish_early("cancelled")

    def failed(self) -> None:
        self._finish_early("failed")

    def _finish_early(self, status: str) -> None:
        if self._turn_status != "active":
            return
        self._turn_status = status
        self._log_summary()

    def _log_aanya_once(self) -> None:
        if self._aanya_logged:
            return
        response = _conversation_line("".join(self._response_parts))
        if response:
            self._logger.info(
                "[AIRA CONVERSATION] session_id=%s turn_id=%s\nAANYA: %s",
                _safe_field(self.session_id),
                _safe_field(self.turn_id),
                response,
            )
        self._aanya_logged = True

    def _log_summary_if_ready(self) -> None:
        if self._turn_status != "completed":
            return
        if self._mode != "none" and self._retrieval_status == "pending":
            return
        self._log_summary()

    def _log_summary(self) -> None:
        if self._summary_logged:
            return
        values = self.metrics.snapshot()
        first_audio_us = self.metrics.boundary_monotonic_us(
            "first_audio_binary_sent"
        )
        retrieval_before_audio: str
        if self._retrieval_completed_us is None or first_audio_us is None:
            retrieval_before_audio = "not_available"
        else:
            retrieval_before_audio = str(
                self._retrieval_completed_us <= first_audio_us
            ).lower()

        lines = [
            "[AIRA TURN SUMMARY] "
            f"session_id={_safe_field(self.session_id)} "
            f"turn_id={_safe_field(self.turn_id)}",
            f"status={self._turn_status}",
            f"mode={self._mode}",
            f"stt_ms={_metric(values, 'stt_ms')}",
            f"llm_first_token_ms={_metric(values, 'llm_ttft_ms')}",
            f"first_speakable_ms={_metric(values, 'ttfs_ms')}",
            f"tts_first_pcm_ms={_metric(values, 'tts_to_first_pcm_ms')}",
            f"ttfa_ms={_metric(values, 'ttfa_ms')}",
            f"response_start_ms={_metric(values, 'ttfa_ms')}",
            f"retrieval_status={self._retrieval_status}",
            "retrieval_ms="
            f"{_format_ms(self._retrieval_ms)}",
            "retrieval_finished_before_first_audio="
            f"{retrieval_before_audio}",
        ]
        self._logger.info("\n".join(lines))
        self._summary_logged = True


_CURRENT_REALTIME_DEBUG_TURN: ContextVar[
    RealtimeConversationDebugTurn | None
] = ContextVar("aira_current_realtime_debug_turn", default=None)
_CURRENT_REALTIME_METRICS: ContextVar[
    RealtimeLatencyMetrics | None
] = ContextVar("aira_current_realtime_metrics", default=None)


class RealtimeConversationMonitor:
    def __init__(
        self,
        enabled: bool,
        *,
        logger: logging.Logger = _LOGGER,
    ) -> None:
        self._enabled = enabled
        self._logger = logger

    @contextmanager
    def turn(
        self,
        *,
        session_id: str,
        turn_id: str,
        generation: int,
        metrics: RealtimeLatencyMetrics,
    ) -> Iterator[RealtimeConversationDebugTurn | None]:
        debug_turn = (
            RealtimeConversationDebugTurn(
                session_id=session_id,
                turn_id=turn_id,
                generation=generation,
                metrics=metrics,
                logger=self._logger,
            )
            if self._enabled
            else None
        )
        metrics_token = _CURRENT_REALTIME_METRICS.set(metrics)
        debug_token = _CURRENT_REALTIME_DEBUG_TURN.set(debug_turn)
        try:
            yield debug_turn
        finally:
            _CURRENT_REALTIME_DEBUG_TURN.reset(debug_token)
            _CURRENT_REALTIME_METRICS.reset(metrics_token)


def current_realtime_debug_turn() -> RealtimeConversationDebugTurn | None:
    return _CURRENT_REALTIME_DEBUG_TURN.get()


def mark_current_realtime_boundary(name: str) -> None:
    metrics = _CURRENT_REALTIME_METRICS.get()
    if metrics is not None:
        metrics.mark_boundary(name)


def debug_retrieval_mode(mode: str) -> None:
    turn = current_realtime_debug_turn()
    if turn is not None:
        turn.set_retrieval_mode(mode)


def debug_retrieval_started(mode: str) -> None:
    mark_current_realtime_boundary("retrieval_start")
    turn = current_realtime_debug_turn()
    if turn is not None:
        turn.retrieval_started(mode)


def debug_retrieval_finished(
    *,
    status: str,
    elapsed_ms: float,
    completed_us: int,
) -> None:
    metrics = _CURRENT_REALTIME_METRICS.get()
    if metrics is not None:
        metrics.mark_boundary(
            "retrieval_complete",
            monotonic_us=completed_us,
        )
    turn = current_realtime_debug_turn()
    if turn is not None:
        turn.retrieval_finished(
            status=status,
            elapsed_ms=elapsed_ms,
            completed_us=completed_us,
        )


def _conversation_line(value: str) -> str:
    return " ".join(value.split()).strip()


def _safe_mode(mode: str) -> str:
    return mode if mode in {
        "none",
        "background_only",
        "current_turn_required",
    } else "unknown"


def _safe_status(status: str) -> str:
    return status if status in {
        "complete",
        "failure",
        "cancelled",
    } else "unknown"


def _format_ms(value: float | None) -> str:
    return "not_available" if value is None else f"{value:.3f}"


def _metric(values: Mapping[str, float], name: str) -> str:
    return _format_ms(values.get(name))
