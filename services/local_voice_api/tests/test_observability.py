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
    DEBUG_CONVERSATION_ENVIRONMENT,
    RealtimeConversationMonitor,
    RealtimeLatencyMetrics,
    TurnTiming,
    current_turn_timing,
    debug_conversation_enabled,
)


class MutableClock:
    """Small injected monotonic clock that never sleeps."""

    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def _render_logs(logger: mock.Mock) -> str:
    rendered: list[str] = []
    for call in logger.info.call_args_list:
        template, *arguments = call.args
        rendered.append(template % tuple(arguments) if arguments else template)
    return "\n".join(rendered)


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

    def test_stage_breakdown_reports_only_same_process_measured_pairs(self) -> None:
        clock = MutableClock(10.0)
        metrics = RealtimeLatencyMetrics("aanya_rt_stages", clock=clock)
        metrics.start_end_of_turn()
        metrics.mark_boundary("stt_start", monotonic_us=10_100_000)
        metrics.mark_boundary("stt_complete", monotonic_us=10_450_000)
        metrics.mark_boundary("llm_request_start", monotonic_us=10_500_000)
        metrics.mark_boundary("llm_first_token", monotonic_us=10_620_000)

        self.assertEqual(
            {
                "end_of_turn_to_stt_start_ms": 100.0,
                "stt_ms": 350.0,
                "stt_to_llm_start_ms": 50.0,
                "llm_ttft_ms": 120.0,
            },
            metrics.stage_breakdown(),
        )
        self.assertNotIn("network/client_delivery_ms", metrics.snapshot())

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

    def test_realtime_event_is_correlated_and_uses_the_injected_clock(self) -> None:
        clock = MutableClock(12.345678)
        metrics = RealtimeLatencyMetrics("aanya_rt_safe", clock=clock)
        logger = mock.Mock(spec=logging.Logger)

        metrics.log_event(
            logger,
            "stt_start",
            session_id="session_safe",
            generation=3,
            input_bytes=8000,
        )

        call = logger.info.call_args
        self.assertIn("event=%s", call.args[0])
        self.assertEqual("stt_start", call.args[1])
        self.assertEqual("session_safe", call.args[2])
        self.assertEqual("aanya_rt_safe", call.args[3])
        self.assertEqual(3, call.args[4])
        self.assertEqual(12_345_678, call.args[5])
        self.assertIn("input_bytes=8000", call.args[6])

    def test_realtime_event_accepts_an_observed_monotonic_boundary(self) -> None:
        metrics = RealtimeLatencyMetrics("aanya_rt_safe", clock=MutableClock(99.0))
        logger = mock.Mock(spec=logging.Logger)

        metrics.log_event(
            logger,
            "stt_complete",
            session_id="session_safe",
            generation=4,
            monotonic_us=12_345,
            stt_duration_ms=321.5,
        )

        call = logger.info.call_args
        self.assertEqual(12_345, call.args[5])
        self.assertIn("stt_duration_ms=321.5", call.args[6])


class RealtimeConversationMonitorTests(unittest.TestCase):
    def test_debug_flag_is_off_by_default_and_logs_no_transcript(self) -> None:
        self.assertFalse(debug_conversation_enabled({}))
        self.assertFalse(
            debug_conversation_enabled(
                {DEBUG_CONVERSATION_ENVIRONMENT: "0"}
            )
        )
        logger = mock.Mock(spec=logging.Logger)
        metrics = RealtimeLatencyMetrics("turn_private", clock=MutableClock())
        monitor = RealtimeConversationMonitor(False, logger=logger)

        with monitor.turn(
            session_id="session_private",
            turn_id="turn_private",
            generation=1,
            metrics=metrics,
        ) as debug_turn:
            self.assertIsNone(debug_turn)

        logger.info.assert_not_called()

    def test_debug_monitor_emits_user_aanya_and_existing_timing_data(self) -> None:
        self.assertTrue(
            debug_conversation_enabled(
                {DEBUG_CONVERSATION_ENVIRONMENT: "1"}
            )
        )
        clock = MutableClock(10.0)
        metrics = RealtimeLatencyMetrics("turn_visible", clock=clock)
        metrics.start_end_of_turn()
        metrics.mark_boundary("stt_start", monotonic_us=10_100_000)
        metrics.mark_boundary("stt_complete", monotonic_us=10_300_000)
        metrics.mark_boundary("llm_request_start", monotonic_us=10_350_000)
        metrics.mark_boundary("llm_first_token", monotonic_us=10_450_000)
        clock.value = 10.45
        metrics.mark_first_llm_token()
        clock.value = 10.60
        metrics.mark_first_speakable_chunk()
        metrics.mark_boundary("tts_queue_enter", monotonic_us=10_610_000)
        metrics.mark_boundary("tts_first_pcm", monotonic_us=10_700_000)
        clock.value = 10.70
        metrics.mark_first_audio_sample()
        metrics.mark_boundary(
            "first_audio_binary_sent", monotonic_us=10_720_000
        )
        clock.value = 10.72
        metrics.mark_first_audio_sent()
        clock.value = 11.0
        metrics.mark_complete()
        logger = mock.Mock(spec=logging.Logger)
        monitor = RealtimeConversationMonitor(True, logger=logger)

        with monitor.turn(
            session_id="session_visible",
            turn_id="turn_visible",
            generation=2,
            metrics=metrics,
        ) as debug_turn:
            self.assertIsNotNone(debug_turn)
            assert debug_turn is not None
            debug_turn.log_user("Did you see the Nepal news?")
            debug_turn.add_response_text("Yeah... ")
            debug_turn.add_response_text("let me check the latest.")
            debug_turn.set_retrieval_mode("none")
            debug_turn.completed()

        logs = _render_logs(logger)
        self.assertIn("USER: Did you see the Nepal news?", logs)
        self.assertIn("AANYA: Yeah... let me check the latest.", logs)
        self.assertIn("status=completed", logs)
        self.assertIn("stt_ms=200.000", logs)
        self.assertIn("llm_first_token_ms=100.000", logs)
        self.assertIn("first_speakable_ms=600.000", logs)
        self.assertIn("tts_first_pcm_ms=90.000", logs)
        self.assertIn("ttfa_ms=720.000", logs)
        self.assertIn("response_start_ms=720.000", logs)

    def test_cancelled_turn_is_marked_without_completed_aanya_text(self) -> None:
        logger = mock.Mock(spec=logging.Logger)
        metrics = RealtimeLatencyMetrics("turn_cancelled", clock=MutableClock())
        metrics.start_end_of_turn()
        monitor = RealtimeConversationMonitor(True, logger=logger)

        with monitor.turn(
            session_id="session_cancelled",
            turn_id="turn_cancelled",
            generation=3,
            metrics=metrics,
        ) as debug_turn:
            assert debug_turn is not None
            debug_turn.log_user("Give me the latest update.")
            debug_turn.add_response_text("Let me check.")
            debug_turn.set_retrieval_mode("current_turn_required")
            debug_turn.cancelled()

        logs = _render_logs(logger)
        self.assertIn("[AIRA TURN SUMMARY]", logs)
        self.assertIn("status=cancelled", logs)
        self.assertIn("mode=current_turn_required", logs)
        self.assertNotIn("AANYA:", logs)


if __name__ == "__main__":
    unittest.main()
