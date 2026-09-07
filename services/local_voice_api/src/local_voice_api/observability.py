"""Dependency-light, monotonic latency instrumentation for local voice turns."""

from __future__ import annotations

import logging
import math
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

    def start_end_of_turn(self) -> None:
        if self._end_of_turn_at is not None:
            raise RuntimeError("End-of-turn timing has already started.")
        self._end_of_turn_at = self._clock()

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
        return dict(self._milestones)

    def log(self, logger: logging.Logger = _LOGGER) -> None:
        fields = " ".join(
            f"{key}={value:.3f}" for key, value in sorted(self._milestones.items())
        )
        logger.info("[AIRA TTFA] turn_id=%s %s", self.turn_id, fields)
