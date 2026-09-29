"""Persona layering: distinct adult companions over fixed safety and grounding."""

from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = SERVICE_ROOT / "src"
sys.path.insert(0, str(SOURCE_ROOT))
CATALOG_PATH = SERVICE_ROOT.parents[1] / "apps" / "mobile" / "assets" / "data" / "companions.json"

from local_voice_api.companion_personas import (  # noqa: E402
    GROUNDING_RULES,
    OUTPUT_RULES,
    PERSONAS,
    SAFETY_RULES,
    ContentMode,
    PersonaTraits,
    build_system_prompt,
)
from local_voice_api.conversation import AANYA_SYSTEM_PERSONA  # noqa: E402
from local_voice_api.retrieval import (  # noqa: E402
    RetrievalResult,
    _grounded_sentence_supported,
)

# The llama-cli path runs with a 2,048-token context shared with retrieval
# references, bounded history, and the reply budget.
MAX_SYSTEM_PROMPT_CHARACTERS = 4_700


def _catalog() -> list[dict[str, object]]:
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


class PersonaCatalogTests(unittest.TestCase):
    def test_every_catalog_companion_has_exactly_one_persona(self) -> None:
        catalog = {entry["id"]: entry["name"] for entry in _catalog()}

        self.assertEqual(20, len(PERSONAS))
        self.assertEqual(set(catalog), set(PERSONAS))
        for companion_id, persona in PERSONAS.items():
            with self.subTest(companion=companion_id):
                self.assertEqual(catalog[companion_id], persona.name)

    def test_all_twenty_personalities_are_distinct(self) -> None:
        personas = list(PERSONAS.values())

        self.assertEqual(20, len({persona.traits for persona in personas}))
        self.assertEqual(20, len({persona.voice for persona in personas}))
        self.assertEqual(20, len({persona.tone for persona in personas}))

    def test_personality_dials_span_the_range(self) -> None:
        for dial in ("flirtatiousness", "teasing", "shyness", "energy", "sass"):
            values = {getattr(persona.traits, dial) for persona in PERSONAS.values()}
            with self.subTest(dial=dial):
                self.assertGreaterEqual(max(values) - min(values), 3)

    def test_trait_dials_are_validated(self) -> None:
        with self.assertRaises(ValueError):
            PersonaTraits(0, 3, 3, 3, 3, 3, 3, 3, 3, 3)
        with self.assertRaises(ValueError):
            PersonaTraits(3, 3, 3, 3, 3, 3, 3, 3, 3, 6)


class PersonaPromptTests(unittest.TestCase):
    def test_aanya_runtime_persona_is_the_composed_prompt(self) -> None:
        self.assertEqual(build_system_prompt("aanya"), AANYA_SYSTEM_PERSONA)

    def test_every_prompt_is_honest_about_being_an_adult_ai(self) -> None:
        for companion_id, persona in PERSONAS.items():
            prompt = build_system_prompt(companion_id).casefold()
            with self.subTest(companion=companion_id):
                self.assertIn(
                    f"you are {persona.name.casefold()}, an adult fictional ai "
                    "companion, never a human",
                    prompt,
                )
                self.assertIn(
                    "if asked whether you are ai or human, clearly and truthfully "
                    "say you are an ai, not a human",
                    prompt,
                )
                self.assertIn(
                    "unless the user explicitly asks", prompt
                )
                self.assertIn("never invent real-world actions", prompt)

    def test_each_prompt_names_only_its_own_companion(self) -> None:
        for companion_id, persona in PERSONAS.items():
            prompt = build_system_prompt(companion_id)
            others = [
                other.name
                for other in PERSONAS.values()
                if other.companion_id != companion_id
            ]
            with self.subTest(companion=companion_id):
                self.assertIn(f"you may truthfully say you are {persona.name}", prompt)
                for name in others:
                    # Whole words only: "Riya" is a substring of "Priya".
                    self.assertIsNone(re.search(rf"\b{name}\b", prompt))

    def test_safety_grounding_and_output_layers_are_identical_everywhere(self) -> None:
        for companion_id in PERSONAS:
            prompt = build_system_prompt(companion_id)
            with self.subTest(companion=companion_id):
                self.assertIn(GROUNDING_RULES, prompt)
                self.assertIn(SAFETY_RULES, prompt)
                self.assertTrue(prompt.endswith(OUTPUT_RULES))

    def test_restored_safety_wording_is_present(self) -> None:
        safety = SAFETY_RULES.casefold()

        self.assertIn("never diagnose or prescribe", safety)
        self.assertIn("qualified professional", safety)
        self.assertIn("red-flag symptoms", safety)
        self.assertIn("not a therapist, crisis worker, or emergency service", safety)
        self.assertIn("emergency services", safety)
        self.assertIn("never encourage emotional dependency or exclusivity", safety)
        self.assertIn("needs only you or must not leave", safety)
        self.assertIn("withdraw from other people", safety)

    def test_core_mode_allows_invited_romance_but_stays_non_graphic(self) -> None:
        prompt = build_system_prompt("maya").casefold()

        for style in ("romantic", "flirtatious", "seductive", "sensual", "teasing"):
            with self.subTest(style=style):
                self.assertIn(style, prompt)
        self.assertIn("only when the user's words invite it", prompt)
        self.assertIn("rather than escalating first", prompt)
        self.assertIn("non-graphic", prompt)
        self.assertIn("no explicit sexual description", prompt)
        self.assertIn("under 18, stop all flirting", prompt)
        self.assertIn("real human partner", prompt)

    def test_only_the_core_content_mode_is_implemented(self) -> None:
        self.assertEqual(["core"], [mode.value for mode in ContentMode])
        with self.assertRaises(ValueError):
            build_system_prompt("aanya", mode="explicit")  # type: ignore[arg-type]

    def test_unknown_companion_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_system_prompt("stranger")

    def test_prompts_fit_the_local_context_budget(self) -> None:
        for companion_id in PERSONAS:
            with self.subTest(companion=companion_id):
                self.assertLessEqual(
                    len(build_system_prompt(companion_id)),
                    MAX_SYSTEM_PROMPT_CHARACTERS,
                )


class FlirtatiousToneDoesNotBypassGroundingTests(unittest.TestCase):
    QUERY = "Who won the cricket match?"
    EVIDENCE = (
        RetrievalResult(
            title="India beat Australia by 6 wickets in Perth Test",
            url="https://example.com/cricket",
            snippet="India chased the target on Sunday to win by 6 wickets in Perth.",
        ),
    )

    def test_supported_answer_passes_plain_or_flirty(self) -> None:
        for sentence in (
            "India beat Australia by 6 wickets in Perth.",
            "India beat Australia by 6 wickets in Perth, handsome.",
        ):
            with self.subTest(sentence=sentence):
                self.assertTrue(
                    _grounded_sentence_supported(self.QUERY, sentence, self.EVIDENCE)
                )

    def test_flirty_wording_cannot_carry_invented_numbers_or_claims(self) -> None:
        for sentence in (
            "India won by 9 wickets, babe.",
            "Kohli smashed a century and the whole crowd chanted your name, love.",
        ):
            with self.subTest(sentence=sentence):
                self.assertFalse(
                    _grounded_sentence_supported(self.QUERY, sentence, self.EVIDENCE)
                )


if __name__ == "__main__":
    unittest.main()
