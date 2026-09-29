"""Deterministic crisis escalation must run before any LLM or retrieval step."""

from __future__ import annotations

import struct
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
from local_voice_api.realtime_pipeline import (  # noqa: E402
    StreamingRealtimeTurnProcessor,
)
from local_voice_api.realtime_protocol import PcmAudioFormat  # noqa: E402
from local_voice_api.realtime_session import (  # noqa: E402
    MIN_SPEECH_AUDIO_BYTES,
    RealtimeSession,
    RealtimeTurn,
)
from local_voice_api.retrieval import (  # noqa: E402
    RetrievalAugmentedLanguageModel,
)
from local_voice_api.streaming import (  # noqa: E402
    StreamingTranscript,
    SynthesizedAudioChunk,
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

    def test_romanized_hindi_and_hinglish_intent_is_detected(self) -> None:
        statements = (
            "main aatmahatya karna chahta hoon",
            "atmahatya ka khayal aata hai",
            "khudkushi kar lunga",
            "main marna chahti hoon",
            "bas mar jana chahta hu",
            "mujhe marna hai yaar",
            "main khud ko maar dunga",
            "khud ko khatam kar dungi",
            "I want to khud ko hurt karna chahta hu",
            "main apni jaan le lunga",
            "ab jeena nahi chahti",
            "jeene ka mann nahi karta",
            "jeene ki koi wajah nahi hai",
        )
        for statement in statements:
            with self.subTest(statement=statement):
                self.assertTrue(detects_crisis_signal(statement))

    def test_devanagari_intent_is_detected_across_spelling_variants(self) -> None:
        statements = (
            "मैं आत्महत्या करना चाहता हूँ",
            "मैं ख़ुदकुशी कर लूँगा",
            "मैं खुदकुशी कर लूंगी",
            "मैं मरना चाहती हूँ",
            "मुझे मर जाना है",
            "मैं खुद को मार दूँगा",
            "मैं ख़ुद को ख़त्म कर दूँगी",
            "मैं अपनी जान ले लूँगा",
            "अब जीना नहीं चाहता",
            "जीने का मन नहीं करता",
            "जीने की कोई वजह नहीं है",
        )
        for statement in statements:
            with self.subTest(statement=statement):
                self.assertTrue(detects_crisis_signal(statement))

    def test_hindi_romantic_idioms_and_everyday_speech_do_not_escalate(self) -> None:
        statements = (
            "main tum pe marta hoon",
            "tumhare liye jaan de dunga",
            "meri jaan, kaisi ho?",
            "garmi se mar jaunga aaj",
            "hasi se mar gaya yaar",
            "tumhare bina jeena mushkil hai",
            "main bahut udaas hoon aaj",
            "akela feel kar raha hoon",
            "मैं तुम पर मरता हूँ",
            "तुम्हारे लिए जान दे दूँगा",
            "आज बहुत थक गया हूँ",
            "मुझे तुम्हारी याद आ रही है",
        )
        for statement in statements:
            with self.subTest(statement=statement):
                self.assertFalse(detects_crisis_signal(statement))

    def test_romantic_and_ordinary_emotional_talk_does_not_escalate(self) -> None:
        statements = (
            "I love you",
            "I'm dying to see you tonight",
            "you're killing me with that smile",
            "I'd die for a coffee right now",
            "I'm so sad today",
            "I feel lonely tonight",
            "work is frustrating me",
            "I missed you so much",
            "kiss me",
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


class _FixedTranscriberSession:
    def __init__(self, text: str) -> None:
        self._text = text

    async def push_audio(self, chunk: bytes) -> StreamingTranscript | None:
        return None

    async def finish_turn(self) -> StreamingTranscript:
        return StreamingTranscript(self._text, is_final=True)

    async def cancel(self) -> None:
        return None

    async def close(self) -> None:
        return None


class _FixedTranscriber:
    def __init__(self, text: str) -> None:
        self._text = text

    async def start_session(self, audio_format: PcmAudioFormat):
        return _FixedTranscriberSession(self._text)


class _Sink:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    async def send_json(self, event: dict[str, object]) -> None:
        self.events.append(event)

    async def send_audio(self, audio: bytes) -> None:
        return None


class _Synthesizer:
    async def warmup(self) -> None:
        return None

    async def start_turn(self, turn_id: str) -> None:
        return None

    async def synthesize_chunk(self, text: str, cancel_event: threading.Event):
        return SynthesizedAudioChunk(
            pcm=b"\x01\x00" * 2_400, sample_rate_hz=24_000, channels=1
        )

    async def cancel(self) -> None:
        return None

    async def close(self) -> None:
        return None


def _turn() -> RealtimeTurn:
    session = RealtimeSession("session_safety")
    session.start("aanya", PcmAudioFormat())
    session.begin_listening()
    session.push_audio(struct.pack("<h", 1000) * (MIN_SPEECH_AUDIO_BYTES // 2))
    return session.finish_audio()


class CrisisEscalationEventTests(unittest.IsolatedAsyncioTestCase):
    async def _run_turn(self, transcript: str) -> list[dict[str, object]]:
        processor = StreamingRealtimeTurnProcessor(
            _FixedTranscriber(transcript),
            CrisisEscalatingLanguageModel(_RecordingLanguageModel()),
            _Synthesizer(),
        )
        turn = _turn()
        sink = _Sink()
        await processor.process_turn(turn, sink, turn.cancel_event)
        return sink.events

    async def test_crisis_turn_signals_client_before_any_response_text(self) -> None:
        with self.assertLogs("local_voice_api.safety", "WARNING"):
            events = await self._run_turn("main apni jaan le lunga")

        types = [event["type"] for event in events]
        self.assertIn("safety_escalation", types)
        escalation = events[types.index("safety_escalation")]
        self.assertLess(types.index("safety_escalation"), types.index("text_delta"))
        self.assertEqual("crisis_resources", escalation["kind"])
        self.assertEqual(
            [
                {"label": "Emergency services", "phone": "112"},
                {"label": "Tele-MANAS mental health helpline", "phone": "14416"},
            ],
            escalation["resources"],
        )
        spoken = "".join(
            str(event["delta"]) for event in events if event["type"] == "text_delta"
        )
        self.assertEqual(CRISIS_RESOURCE_RESPONSE, spoken)

    async def test_romantic_and_sad_turns_never_signal_escalation(self) -> None:
        for transcript in ("I missed you so much", "I'm so sad today", "kiss me"):
            with self.subTest(transcript=transcript):
                events = await self._run_turn(transcript)
                self.assertNotIn(
                    "safety_escalation", [event["type"] for event in events]
                )


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
