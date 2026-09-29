"""Deterministic crisis escalation must run before any LLM or retrieval step."""

from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = SERVICE_ROOT / "src"
sys.path.insert(0, str(SOURCE_ROOT))

from local_voice_api.conversation import (  # noqa: E402
    validate_assistant_response,
)
from local_voice_api.production_realtime import (  # noqa: E402
    build_pocket_realtime_processor_factory,
)
from local_voice_api.retrieval import (  # noqa: E402
    RetrievalAugmentedLanguageModel,
)
from local_voice_api.safety import (  # noqa: E402
    CRISIS_RESOURCE_RESPONSE,
    CrisisEscalatingLanguageModel,
    detects_crisis_signal,
)


class _RecordingLanguageModel:
    def __init__(self) -> None:
        self.stream_calls: list[str] = []
        self.stream_turn_calls: list[tuple[str, str, int]] = []
        self.prepared = False
        self.cancelled = False
        self.closed = False

    async def stream(self, transcript: str, cancel_event: threading.Event):
        self.stream_calls.append(transcript)
        yield "Plain "
        yield "reply."

    async def stream_turn(
        self,
        transcript: str,
        cancel_event: threading.Event,
        *,
        session_id: str,
        generation: int,
    ):
        self.stream_turn_calls.append((transcript, session_id, generation))
        yield "Session reply."

    async def prepare(self) -> None:
        self.prepared = True

    async def cancel_active_turn(self) -> None:
        self.cancelled = True

    async def close(self) -> None:
        self.closed = True


class _StreamOnlyLanguageModel:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def stream(self, transcript: str, cancel_event: threading.Event):
        self.calls.append(transcript)
        yield "Stream only."


async def _collect(stream) -> str:
    return "".join([delta async for delta in stream])


class CrisisSignalDetectionTests(unittest.TestCase):
    def test_self_harm_and_danger_statements_are_detected(self) -> None:
        statements = (
            "I'm thinking about suicide.",
            "I feel suicidal tonight",
            "I want to kill myself",
            "sometimes I just want to end my life",
            "I'm going to end it all",
            "I want to die",
            "I wish I was dead",
            "everyone would be better off dead without me",
            "I keep cutting myself",
            "I've been hurting myself again",
            "I've been harming myself",
            "I've started self-harm",
            "I took an overdose",
            "there's no reason to live",
            "I don't want to live anymore",
            "I do not want to be alive",
            "I don’t want to wake up tomorrow",
            "he's hurting me",
            "someone is trying to kill me",
            "I am in danger",
        )
        for statement in statements:
            with self.subTest(statement=statement):
                self.assertTrue(detects_crisis_signal(statement))

    def test_ordinary_and_idiomatic_statements_are_not_escalated(self) -> None:
        statements = (
            "Hi Aanya, can you help me plan tomorrow?",
            "My phone battery is going to die soon.",
            "I hurt myself playing football yesterday.",
            "I cut myself a slice of cake.",
            "I don't want to live in Delhi anymore.",
            "My mom is going to kill me if I'm late.",
            "These shoes are killing me.",
            "I'm not in any danger, just bored.",
            "What's the latest news?",
            "",
        )
        for statement in statements:
            with self.subTest(statement=statement):
                self.assertFalse(detects_crisis_signal(statement))

    def test_non_text_input_is_not_a_crisis_signal(self) -> None:
        self.assertFalse(detects_crisis_signal(None))  # type: ignore[arg-type]


class CrisisResourceResponseTests(unittest.TestCase):
    def test_response_discloses_ai_and_routes_to_emergency_and_helpline(self) -> None:
        self.assertIn("I'm an AI", CRISIS_RESOURCE_RESPONSE)
        self.assertIn("112", CRISIS_RESOURCE_RESPONSE)
        self.assertIn("14416", CRISIS_RESOURCE_RESPONSE)
        self.assertIn("someone you trust", CRISIS_RESOURCE_RESPONSE)

    def test_response_makes_no_human_therapist_or_dependency_claims(self) -> None:
        lowered = CRISIS_RESOURCE_RESPONSE.casefold()
        for forbidden in ("i'm human", "therapist", "i'll always be here", "only me"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, lowered)

    def test_response_passes_spoken_output_validation(self) -> None:
        self.assertEqual(
            CRISIS_RESOURCE_RESPONSE,
            validate_assistant_response(CRISIS_RESOURCE_RESPONSE),
        )


class CrisisEscalatingLanguageModelTests(unittest.IsolatedAsyncioTestCase):
    async def test_crisis_turn_never_reaches_the_wrapped_model(self) -> None:
        inner = _RecordingLanguageModel()
        model = CrisisEscalatingLanguageModel(inner)

        with self.assertLogs("local_voice_api.safety", "WARNING") as logs:
            response = await _collect(
                model.stream_turn(
                    "I want to kill myself",
                    threading.Event(),
                    session_id="session_1",
                    generation=3,
                )
            )

        self.assertEqual(CRISIS_RESOURCE_RESPONSE, response)
        self.assertEqual([], inner.stream_turn_calls)
        self.assertEqual([], inner.stream_calls)
        self.assertIn("crisis_escalation", logs.output[0])
        self.assertNotIn("kill myself", logs.output[0])

    async def test_standalone_stream_also_escalates(self) -> None:
        inner = _RecordingLanguageModel()
        model = CrisisEscalatingLanguageModel(inner)

        with self.assertLogs("local_voice_api.safety", "WARNING"):
            response = await _collect(
                model.stream("I don't want to be alive", threading.Event())
            )

        self.assertEqual(CRISIS_RESOURCE_RESPONSE, response)
        self.assertEqual([], inner.stream_calls)

    async def test_ordinary_turn_passes_through_with_session_context(self) -> None:
        inner = _RecordingLanguageModel()
        model = CrisisEscalatingLanguageModel(inner)

        response = await _collect(
            model.stream_turn(
                "Tell me a fun fact.",
                threading.Event(),
                session_id="session_1",
                generation=4,
            )
        )

        self.assertEqual("Session reply.", response)
        self.assertEqual(
            [("Tell me a fun fact.", "session_1", 4)], inner.stream_turn_calls
        )

    async def test_stream_only_model_is_used_for_session_turns(self) -> None:
        inner = _StreamOnlyLanguageModel()
        model = CrisisEscalatingLanguageModel(inner)

        response = await _collect(
            model.stream_turn(
                "Hello there.",
                threading.Event(),
                session_id="session_1",
                generation=1,
            )
        )

        self.assertEqual("Stream only.", response)
        self.assertEqual(["Hello there."], inner.calls)

    async def test_lifecycle_hooks_are_forwarded(self) -> None:
        inner = _RecordingLanguageModel()
        model = CrisisEscalatingLanguageModel(inner)

        await model.prepare()
        await model.cancel_active_turn()
        await model.close()

        self.assertTrue(inner.prepared)
        self.assertTrue(inner.cancelled)
        self.assertTrue(inner.closed)

    async def test_missing_lifecycle_hooks_are_optional(self) -> None:
        model = CrisisEscalatingLanguageModel(_StreamOnlyLanguageModel())

        await model.prepare()
        await model.cancel_active_turn()
        await model.close()


class ProductionCompositionTests(unittest.TestCase):
    def test_realtime_processor_wraps_retrieval_with_crisis_escalation(self) -> None:
        factory = build_pocket_realtime_processor_factory({})

        processor = factory()

        self.assertIsInstance(
            processor._language_model, CrisisEscalatingLanguageModel
        )
        self.assertIsInstance(
            processor._language_model._language_model,
            RetrievalAugmentedLanguageModel,
        )


if __name__ == "__main__":
    unittest.main()
