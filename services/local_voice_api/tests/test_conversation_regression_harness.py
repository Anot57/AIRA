from __future__ import annotations

import runpy
import unittest
from pathlib import Path


_HARNESS = runpy.run_path(
    str(
        Path(__file__).resolve().parents[1]
        / "tools"
        / "aanya_conversation_regression.py"
    )
)
looks_truncated = _HARNESS["looks_truncated"]
is_only_checking_acknowledgement = _HARNESS[
    "is_only_checking_acknowledgement"
]
RecordingRetrievalProvider = _HARNESS["RecordingRetrievalProvider"]


class ConversationRegressionHarnessTests(unittest.TestCase):
    def test_sentence_end_allows_emoji_quotes_brackets_and_markdown(self) -> None:
        complete = (
            "Because they're all slimy! 😄",
            'That is complete.”',
            "That is complete.)",
            "That is complete.**",
            "Coverage of breaking world stories. NPR",
        )

        for response in complete:
            with self.subTest(response=response):
                self.assertFalse(looks_truncated(response, grounded=True))

    def test_sentence_end_still_detects_obvious_mid_sentence_cutoff(self) -> None:
        self.assertTrue(
            looks_truncated(
                "Rescuers have successfully pulled two",
                grounded=True,
            )
        )

    def test_static_checking_only_detection_does_not_reject_real_answer(
        self,
    ) -> None:
        self.assertTrue(is_only_checking_acknowledgement("Hmm, let me check."))
        self.assertFalse(
            is_only_checking_acknowledgement(
                "Let me think... octopuses have three hearts."
            )
        )


class ConversationRegressionHarnessAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_forced_retrieval_failures_are_recorded_without_live_search(
        self,
    ) -> None:
        provider = RecordingRetrievalProvider(object())
        provider.force_failures(2)

        for query in ("world news", "world news"):
            with self.assertRaises(TimeoutError):
                await provider.search(query, medical=False, limit=6)

        self.assertEqual(2, len(provider.calls))
        self.assertEqual(
            ["TimeoutError", "TimeoutError"],
            [call["error"] for call in provider.calls],
        )


if __name__ == "__main__":
    unittest.main()
