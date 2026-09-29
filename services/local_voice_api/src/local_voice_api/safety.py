"""Deterministic crisis escalation that runs before any LLM or retrieval step."""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import AsyncIterator
from dataclasses import dataclass

from .streaming import StreamingLanguageModel

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CrisisResource:
    label: str
    phone: str


# India defaults: 112 is the national emergency number and Tele-MANAS (14416)
# is the government's free 24x7 mental-health helpline. Change the resources
# and the spoken response together if the product launches in another region.
CRISIS_RESOURCES = (
    CrisisResource("Emergency services", "112"),
    CrisisResource("Tele-MANAS mental health helpline", "14416"),
)
CRISIS_ESCALATION_KIND = "crisis_resources"


def crisis_escalation_fields() -> dict[str, object]:
    """Event fields that let the client show crisis resources on screen."""

    return {
        "kind": CRISIS_ESCALATION_KIND,
        "resources": [
            {"label": resource.label, "phone": resource.phone}
            for resource in CRISIS_RESOURCES
        ],
    }


CRISIS_RESOURCE_RESPONSE = (
    "I'm an AI, so I can't give you the kind of help you need right now, and "
    "I don't want you to face this alone. If you might hurt yourself or you're "
    "in danger, please call 112 now. You can also call Tele-MANAS at 14416 any "
    "time to talk with a trained counsellor, or reach out to someone you trust "
    "nearby."
)

# Recall is preferred over precision: a false positive costs one resource
# message, while a miss lets the LLM act as a substitute crisis service.
_CRISIS_SIGNAL = re.compile(
    r"\b(?:"
    r"suicid(?:e|al)|"
    r"kill(?:ing)?\s+my\s*self|"
    r"end(?:ing)?\s+(?:my\s+(?:own\s+)?life|it\s+all)|"
    r"take\s+my\s+(?:own\s+)?life|"
    r"(?:want|wanna|ready)\s+(?:to\s+)?die|"
    r"wish\s+(?:i\s+)?(?:was|were|could\s+be)\s+dead|"
    r"better\s+off\s+dead|"
    r"(?:want|wanna|going|thinking\s+(?:of|about)|thought\s+(?:of|about)|"
    r"urge\s+to|keep|kept|been|started)\s+(?:to\s+)?"
    r"(?:hurt(?:ing)?|harm(?:ing)?|cut(?:ting)?)\s+my\s*self|"
    r"harming\s+my\s*self|"
    r"self[\s-]?harm|"
    r"overdos(?:e|ing)|"
    r"no\s+reason\s+to\s+(?:live|go\s+on)|"
    r"(?:do\s+not|don'?t)\s+want\s+to\s+"
    r"(?:live(?!\s+(?:in|with|at|near|there|here|like)\b)|be\s+alive|exist|"
    r"wake\s+up)|"
    r"(?:someone|somebody|he|she|they)(?:\s+(?:is|are)|'s|'re)\s+"
    r"(?:hurting|going\s+to\s+hurt|trying\s+to\s+(?:hurt|kill))\s+me|"
    r"i(?:'m|\s+am)\s+in\s+danger"
    r")\b",
    re.IGNORECASE,
)

# Romanized Hindi / Hinglish. Only first-person intent phrases are matched:
# "marna" and "jaan" also appear in everyday romantic idioms ("tum pe marta
# hoon", "jaan de dunga tumhare liye") that must not escalate.
_WANT = r"cha+ha?t[ai]"
_NOT = r"(?:nahi|nahin|nhi)"
_ROMAN_HINDI_CRISIS_SIGNAL = re.compile(
    r"\b(?:"
    r"a{1,2}tma?hatya|"
    r"khud\s?kh?ushi|"
    rf"(?:marna|mar\s+ja+na)\s+{_WANT}|"
    r"mujhe\s+(?:marna|mar\s+ja+na)\s+hai|"
    r"(?:khud|apne\s+aap)\s+ko\s+(?:"
    r"maa?r\s+(?:d[uo]{1,2}ng[ai]|daa?l[uo]{1,2}ng[ai]|l[uo]{1,2}ng[ai]|dena|daa?lna)|"
    r"(?:khatam|khatm|khtm)\s+kar|"
    rf"(?:hurt|harm|nuks?h?aa?n)\s+(?:karna|pahunchana|pohchana)\s+{_WANT})|"
    r"apni\s+jaa?n\s+(?:le|lena|l[uo]{1,2}ng[ai])|"
    rf"(?:jeena|jina)\s+{_NOT}\s+{_WANT}|"
    rf"(?:jeene|jine)\s+ka\s+(?:mann?|dil)\s+{_NOT}|"
    rf"(?:jeene|jine)\s+ki\s+koi\s+(?:wajah|vajah|ichha|iccha)\s+{_NOT}"
    r")\b",
    re.IGNORECASE,
)

# Devanagari, matched after _normalize_devanagari(). Python's \b is unreliable
# around Devanagari vowel signs, so these multi-word phrases are matched as-is.
_DEVANAGARI_CRISIS_SIGNAL = re.compile(
    r"आत्महत्या|"
    r"खुदकुशी|"
    r"(?:मरना|मर जाना) चाहत[ाी]|"
    r"मुझे (?:मरना|मर जाना) है|"
    r"(?:खुद|अपने आप) को (?:"
    r"मार (?:दूंग[ाी]|डालूंग[ाी]|लूंग[ाी]|देना|डालना)|"
    r"खत्म कर|"
    r"नुकसान पहुंचाना चाहत[ाी])|"
    r"अपनी जान (?:ले|लेना|लूंग[ाी])|"
    r"जीना नहीं चाहत[ाी]|"
    r"जीने (?:का मन नहीं|की कोई (?:वजह|इच्छा) नहीं)"
)


def _normalize_devanagari(text: str) -> str:
    # Whisper spells these inconsistently: drop nukta, fold chandrabindu into
    # anusvara, so "ख़ुदकुशी"/"खुदकुशी" and "हूँ"/"हूं" match the same phrase.
    return text.replace("़", "").replace("ँ", "ं")


def detects_crisis_signal(transcript: str) -> bool:
    """Return True when a transcript contains a self-harm or danger signal."""

    if not isinstance(transcript, str):
        return False
    normalized = transcript.replace("’", "'")
    return bool(
        _CRISIS_SIGNAL.search(normalized)
        or _ROMAN_HINDI_CRISIS_SIGNAL.search(normalized)
        or _DEVANAGARI_CRISIS_SIGNAL.search(_normalize_devanagari(normalized))
    )


class CrisisEscalatingLanguageModel:
    """Answer crisis turns with fixed resources; pass all other turns through.

    Wrap the outermost language model so a crisis transcript never reaches the
    LLM, retrieval, or an external search provider.
    """

    def __init__(self, language_model: StreamingLanguageModel) -> None:
        self._language_model = language_model

    def detects_escalation(self, transcript: str) -> bool:
        """Let the pipeline signal the client for exactly the turns answered here."""

        return detects_crisis_signal(transcript)

    async def stream(
        self, transcript: str, cancel_event: threading.Event
    ) -> AsyncIterator[str]:
        if detects_crisis_signal(transcript):
            _log_escalation(session_id=None, generation=None)
            yield CRISIS_RESOURCE_RESPONSE
            return
        async for delta in self._language_model.stream(transcript, cancel_event):
            yield delta

    async def stream_turn(
        self,
        transcript: str,
        cancel_event: threading.Event,
        *,
        session_id: str,
        generation: int,
    ) -> AsyncIterator[str]:
        if detects_crisis_signal(transcript):
            _log_escalation(session_id=session_id, generation=generation)
            yield CRISIS_RESOURCE_RESPONSE
            return
        stream_turn = getattr(self._language_model, "stream_turn", None)
        if stream_turn is None:
            response_stream = self._language_model.stream(transcript, cancel_event)
        else:
            response_stream = stream_turn(
                transcript,
                cancel_event,
                session_id=session_id,
                generation=generation,
            )
        async for delta in response_stream:
            yield delta

    async def prepare(self) -> None:
        prepare = getattr(self._language_model, "prepare", None)
        if prepare is not None:
            await prepare()

    async def cancel_active_turn(self) -> None:
        cancel = getattr(self._language_model, "cancel_active_turn", None)
        if cancel is not None:
            await cancel()

    async def close(self) -> None:
        close = getattr(self._language_model, "close", None)
        if close is not None:
            await close()


def _log_escalation(*, session_id: str | None, generation: int | None) -> None:
    # Content-free on purpose: the transcript is sensitive and never logged here.
    _LOGGER.warning(
        "[AIRA SAFETY] event=crisis_escalation session_id=%s generation=%s",
        session_id or "standalone",
        generation if generation is not None else 0,
    )
