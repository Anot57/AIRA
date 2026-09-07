"""Deterministic checks for monotonic turn and realtime latency metrics."""

from __future__ import annotations

import logging
import sys
import unittest
from pathlib import Path
from unittest import mock

SERVICE_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = SERVICE_ROOT / "src"
sys.path.insert(0, str(SOURCE_ROOT))

from local_voice_api.observability import (  # noqa: E402
    RealtimeLatencyMetrics,
    TurnTiming,
    current_turn_timing,
)


class MutableClock:
    """Small injected monotonic clock that never sleeps."""

    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class TurnTimingTests(unittest.TestCase):
    def test_stage_and_milestone_use_monotonic_elapsed_time(self) -> None:
        clock = MutableClock(10.0)
        logger = mock.Mock(spec=logging.Logger)
        timing = TurnTiming("aanya_turn_test", clock=clock, logger=logger)

        clock.value = 10.125
        with timing.stage("stt", device="cpu"):
            clock.value = 10.375
        clock.value = 10.5
        milestone = timing.milestone("total", response_characters=42)

        snapshot = timing.snapshot()
        self.assertEqual(2, len(snapshot))
        self.assertEqual("stt", snapshot[0].stage)
        self.assertEqual(250.0, snapshot[0].elapsed_ms)
        self.assertEqual({"device": "cpu"}, snapshot[0].fields)
        self.assertEqual(500.0, milestone.elapsed_ms)
        self.assertEqual({"stt": 250.0, "total": 500.0}, timing.durations_ms())
        logger.info.assert_called()

    def test_context_binding_is_scoped_and_restored(self) -> None:
        timing = TurnTiming("context_test", clock=MutableClock())

        self.assertIsNone(current_turn_timing())
        with timing.bind():
            self.assertIs(timing, current_turn_timing())
        self.assertIsNone(current_turn_timing())

    def test_invalid_measurements_are_rejected_and_negative_clock_is_clamped(
        self,
    ) -> None:
        timing = TurnTiming("clock_test", clock=MutableClock())

        self.assertEqual(0.0, timing.record("safe", -0.5).elapsed_ms)
        for name, duration in (("", 1.0), ("bad", float("inf"))):
            with self.subTest(name=name, duration=duration):
                with self.assertRaises(ValueError):
                    timing.record(name, duration)

    def test_log_fields_redact_paths_and_remove_newlines(self) -> None:
        logger = mock.Mock(spec=logging.Logger)
        timing = TurnTiming("safe_id", clock=MutableClock(), logger=logger)

        timing.record(
            "upload",
            0.001,
            internal_path="/mnt/e/aira-local-runtime/private.wav",
            note="first\nsecond",
        )

        call = logger.info.call_args
        self.assertNotIn("private.wav", str(call))
        self.assertIn("[redacted-path]", str(call))
        self.assertNotIn("first\nsecond", str(call))


class RealtimeLatencyMetricsTests(unittest.TestCase):
    def test_boundaries_are_measured_from_end_of_turn_once(self) -> None:
        clock = MutableClock(20.0)
        metrics = RealtimeLatencyMetrics("aanya_rt_test", clock=clock)

        metrics.start_end_of_turn()
        clock.value = 20.1
        metrics.mark_stt_final()
        clock.value = 20.2
        metrics.mark_first_llm_token()
        clock.value = 20.35
        metrics.mark_first_speakable_chunk()
        clock.value = 20.5
        metrics.mark_first_audio_sample()
        clock.value = 20.55
        metrics.mark_first_audio_sent()
        clock.value = 21.0
        metrics.mark_complete()

        self.assertEqual(
            {
                "stt_final_ms": 100.0,
                "ttft_ms": 200.0,
                "ttfs_ms": 350.0,
                "ttfas_ms": 500.0,
                "ttfa_ms": 550.0,
                "total_ms": 1000.0,
            },
            metrics.snapshot(),
        )

        clock.value = 99.0
        metrics.mark_first_audio_sample()
        metrics.mark_first_audio_sent()
        self.assertEqual(550.0, metrics.snapshot()["ttfa_ms"])

    def test_boundaries_require_exactly_one_end_of_turn_start(self) -> None:
        metrics = RealtimeLatencyMetrics("aanya_rt_test", clock=MutableClock())

        with self.assertRaisesRegex(RuntimeError, "has not started"):
            metrics.mark_first_llm_token()
        metrics.start_end_of_turn()
        with self.assertRaisesRegex(RuntimeError, "already started"):
            metrics.start_end_of_turn()

    def test_log_contains_only_measured_fields(self) -> None:
        clock = MutableClock(1.0)
        metrics = RealtimeLatencyMetrics("aanya_rt_safe", clock=clock)
        logger = mock.Mock(spec=logging.Logger)
        metrics.start_end_of_turn()
        clock.value = 1.25
        metrics.mark_first_llm_token()

        metrics.log(logger)

        logger.info.assert_called_once()
        self.assertIn("ttft_ms", str(logger.info.call_args))
        self.assertNotIn("ttfa_ms", str(logger.info.call_args))


if __name__ == "__main__":
    unittest.main()
