"""Live audio-free acceptance harness for Aanya's conversation layer."""

from __future__ import annotations

import asyncio
import json
import os
import re
import statistics
import threading
import time
from pathlib import Path
from typing import AsyncIterator

from local_voice_api.conversation import AANYA_SYSTEM_PERSONA, DEFAULT_LLM_MAX_TOKENS
from local_voice_api.llama_server import (
    LlamaServerConfig,
    LlamaServerStreamingLanguageModel,
)
from local_voice_api.retrieval import (
    AsyncRetrievalLanguageModel,
    _usable_retrieval_results,
    retrieval_mode_for_query,
    retrieval_provider_from_environment,
)


os.environ.setdefault("AIRA_LLAMA_SERVER_URL", "http://127.0.0.1:8767")
os.environ.setdefault("AIRA_SEARCH_PROVIDER", "searxng")
os.environ.setdefault("AIRA_SEARXNG_URL", "http://127.0.0.1:8888")

REPORT_DIRECTORY = Path("/mnt/e/aira-local-runtime")
LATEST_REPORT_PATH = REPORT_DIRECTORY / "aanya-conversation-regression-latest.json"
RETRIEVAL_FAILURE_TEXT = "couldn't get reliable fresh results"
CHECK_AGAIN = {"check again", "can you check again", "try again", "search again"}
_TRAILING_DECORATION = re.compile(
    r"[\s\"'”’\)\]\}\*_~`\u200d\ufe0f\u2600-\u27bf\U0001f000-\U0001faff]+$"
)
_HELPDESK = (
    "i'm here to help",
    "i am here to help",
    "what can i do for you",
    "how can i help",
    "how i can help",
    "how may i assist",
    "i'm here to support you",
    "what do you need help with",
    "i'm here to listen",
    "i am here to listen",
    "listen and support you",
)
_HELPDESK_SELF_POSITIONING = re.compile(
    r"\bi(?:'m| am)\s+(?:just\s+)?here\s+to\s+(?:help|listen|support)\b",
    re.IGNORECASE,
)
_REFERENTIAL_FOLLOWUP = re.compile(
    r"\b(?:first|second|third)\s+(?:story|one)\b|"
    r"\b(?:that story|what happened(?: next)?|tell me more|what about that|"
    r"any update|then what|why)\b",
    re.IGNORECASE,
)
_FALSE_BIOLOGICAL_SELF_CLAIM = re.compile(
    r"\bi(?:(?:'m| am)(?:\s+feeling)?| feel)\s+"
    r"(?:really\s+|pretty\s+|very\s+|just\s+|so\s+)?"
    r"(?:tired|sleepy|hungry|thirsty|sick|ill|exhausted|hot|cold)\b",
    re.IGNORECASE,
)


def normalize(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9']+", text.casefold()))


def looks_truncated(text: str, *, grounded: bool) -> bool:
    stripped = text.rstrip()
    if not stripped:
        return True
    if not grounded and len(stripped) < 80:
        return False
    meaningful_end = _TRAILING_DECORATION.sub("", stripped)
    if not meaningful_end or meaningful_end[-1] in ".?!…":
        return not meaningful_end
    final_token = re.search(r"[A-Za-z0-9]+$", meaningful_end)
    if final_token is not None and final_token.group().isupper():
        return False
    return True


def is_only_checking_acknowledgement(text: str) -> bool:
    normalized = normalize(text)
    normalized = re.sub(
        r"^(?:sure|yeah|okay|ok|alright|hmm)\s+", "", normalized
    )
    return normalized in {
        "let me check",
        "let me see",
        "one sec",
        "one second",
        "checking",
        "checking now",
        "give me a sec",
        "give me a second",
    }


def checking_phrase_count(text: str) -> int:
    lowered = text.casefold()
    return sum(
        lowered.count(phrase)
        for phrase in (
            "let me check",
            "let me see",
            "one sec",
            "checking",
            "give me a second",
        )
    )


def is_short_near_echo(user_text: str, response: str) -> bool:
    user = normalize(user_text)
    answer = normalize(response)
    if not user or not answer:
        return False
    user_words = user.split()
    answer_words = answer.split()
    if len(user_words) > 12 or len(answer_words) > len(user_words) + 2:
        return False
    if answer_words[: len(user_words)] != user_words:
        return False
    suffix = answer_words[len(user_words) :]
    return not suffix or all(
        word in {"really", "okay", "ok", "yeah", "please"} for word in suffix
    )


def is_surprising_user_text(text: str) -> bool:
    return bool(
        re.search(
            r"\b(?:can't believe|cannot believe|won|lottery|shocked|surpris|"
            r"unbelievable|amazing|incredible|seriously|no way|crashed|exploded)\b",
            text,
            re.IGNORECASE,
        )
    )


class RecordingLanguageModel:
    def __init__(self, inner: LlamaServerStreamingLanguageModel) -> None:
        self.inner = inner
        self.calls: list[dict[str, object]] = []

    async def stream(
        self, transcript: str, cancel_event: threading.Event
    ) -> AsyncIterator[str]:
        async for delta in self._record(
            transcript, cancel_event, max_tokens=DEFAULT_LLM_MAX_TOKENS
        ):
            yield delta

    async def stream_with_token_budget(
        self,
        transcript: str,
        cancel_event: threading.Event,
        *,
        max_tokens: int,
    ) -> AsyncIterator[str]:
        async for delta in self._record(
            transcript, cancel_event, max_tokens=max_tokens
        ):
            yield delta

    async def _record(
        self,
        transcript: str,
        cancel_event: threading.Event,
        *,
        max_tokens: int,
    ) -> AsyncIterator[str]:
        started = time.perf_counter()
        first_token_at: float | None = None
        parts: list[str] = []
        try:
            async for delta in self.inner.stream_with_token_budget(
                transcript, cancel_event, max_tokens=max_tokens
            ):
                if first_token_at is None:
                    first_token_at = time.perf_counter()
                parts.append(delta)
                yield delta
        finally:
            completed = time.perf_counter()
            self.calls.append(
                {
                    "input": transcript,
                    "output": "".join(parts).strip(),
                    "max_tokens": max_tokens,
                    "first_token_ms": None
                    if first_token_at is None
                    else (first_token_at - started) * 1000,
                    "total_ms": (completed - started) * 1000,
                    "request_started_monotonic": started,
                    "first_token_monotonic": first_token_at,
                }
            )

    async def prepare(self) -> None:
        await self.inner.prepare()

    async def close(self) -> None:
        await self.inner.close()


class RecordingRetrievalProvider:
    def __init__(self, inner: object) -> None:
        self.inner = inner
        self.calls: list[dict[str, object]] = []
        self._forced_failures_remaining = 0

    def force_failures(self, count: int) -> None:
        self._forced_failures_remaining = count

    async def search(self, query: str, *, medical: bool, limit: int):
        started = time.perf_counter()
        try:
            if self._forced_failures_remaining:
                self._forced_failures_remaining -= 1
                raise TimeoutError("forced regression retrieval failure")
            results = await self.inner.search(query, medical=medical, limit=limit)
            usable_results = _usable_retrieval_results(query, results)
        except Exception as error:
            self.calls.append(
                {
                    "query": query,
                    "elapsed_ms": (time.perf_counter() - started) * 1000,
                    "result_count": 0,
                    "error": type(error).__name__,
                    "completed_monotonic": time.perf_counter(),
                }
            )
            raise
        self.calls.append(
            {
                    "query": query,
                    "elapsed_ms": (time.perf_counter() - started) * 1000,
                    "result_count": len(usable_results),
                    "raw_result_count": len(results),
                "error": None,
                "completed_monotonic": time.perf_counter(),
            }
        )
        return results


SCENARIOS = (
    (
        "casual",
        ("Hi Aanya", "How are you?", "What's going on?", "Tell me something interesting."),
    ),
    ("affection", ("I love you.", "You love me?", "I love you again.")),
    (
        "anti_parrot",
        ("Yes.", "Okay.", "Really?", "Go!", "Ugh.", "Why?", "What do you mean?"),
    ),
    (
        "nepal_news",
        (
            "Can you tell me the latest Nepal news?",
            "Check again.",
            "Tell me more.",
            "What happened after that?",
        ),
    ),
    (
        "sports",
        ("Can you tell me the latest sports news?", "What about football?", "Tell me more."),
    ),
    (
        "legal",
        (
            "Can you tell me about Donald Trump's latest legal issues?",
            "Check again.",
            "What happened?",
        ),
    ),
    (
        "world_news",
        (
            "Tell me the latest world news.",
            "Tell me about the first story.",
            "What happened next?",
        ),
    ),
    (
        "failed_retrieval_followup",
        (
            "Tell me the latest world news.",
            "Tell me about the first story.",
        ),
    ),
    ("topic_switch", ("Nepal news", "sports news", "Check again")),
    (
        "general_knowledge",
        ("What is Python?", "Why is the sky blue?", "Tell me a joke."),
    ),
)


def _grounded_call(calls: list[dict[str, object]]) -> bool:
    return any("<refs>" in str(call["input"]) for call in calls)


async def run() -> int:
    config = LlamaServerConfig.from_environment(os.environ)
    llm = RecordingLanguageModel(
        LlamaServerStreamingLanguageModel(
            config,
            system_persona=AANYA_SYSTEM_PERSONA,
            max_tokens=DEFAULT_LLM_MAX_TOKENS,
        )
    )
    provider = RecordingRetrievalProvider(retrieval_provider_from_environment(os.environ))
    model = AsyncRetrievalLanguageModel(llm, provider)
    report: list[dict[str, object]] = []
    fail_count = 0
    warn_count = 0
    case_number = 0

    print("\n" + "=" * 78)
    print("AANYA CONVERSATION REGRESSION")
    print("=" * 78)
    print(f"llama={config.base_url} search={os.environ['AIRA_SEARXNG_URL']}")

    try:
        for scenario_number, (scenario_name, turns) in enumerate(SCENARIOS, 1):
            session_id = f"regression_{scenario_number}_{scenario_name}"
            last_effective_query: str | None = None
            last_retrieval_succeeded: bool | None = None
            if scenario_name == "failed_retrieval_followup":
                provider.force_failures(2)
            print(f"\n{'#' * 78}\nSCENARIO: {scenario_name}\n{'#' * 78}")

            for generation, user_text in enumerate(turns, 1):
                case_number += 1
                provider_before = len(provider.calls)
                llm_before = len(llm.calls)
                started = time.perf_counter()
                first_delta_at: float | None = None
                response_parts: list[str] = []
                runtime_error: str | None = None

                try:
                    async for delta in model.stream_turn(
                        user_text,
                        threading.Event(),
                        session_id=session_id,
                        generation=generation,
                    ):
                        if first_delta_at is None:
                            first_delta_at = time.perf_counter()
                        response_parts.append(delta)
                except Exception as error:
                    runtime_error = f"{type(error).__name__}: {error}"

                completed = time.perf_counter()
                response = "".join(response_parts).strip()
                new_provider_calls = provider.calls[provider_before:]
                new_llm_calls = llm.calls[llm_before:]
                declared_mode = retrieval_mode_for_query(user_text).value
                grounded = _grounded_call(new_llm_calls)
                effective_mode = (
                    "current_turn_required"
                    if new_provider_calls
                    else "context_reused"
                    if grounded
                    else declared_mode
                )
                effective_query = (
                    str(new_provider_calls[-1]["query"])
                    if new_provider_calls
                    else last_effective_query
                    if grounded
                    else None
                )
                if effective_query:
                    last_effective_query = effective_query

                first_delta_ms = (
                    None
                    if first_delta_at is None
                    else (first_delta_at - started) * 1000
                )
                total_ms = (completed - started) * 1000
                failures: list[str] = []
                warnings: list[str] = []
                user_norm = normalize(user_text)
                response_norm = normalize(response)

                if runtime_error:
                    failures.append("RUNTIME_ERROR")
                if not response:
                    failures.append("EMPTY_RESPONSE")
                if user_norm and response_norm and user_norm == response_norm:
                    failures.append("PARROT_EXACT")
                elif is_short_near_echo(user_text, response):
                    failures.append("PARROT_NEAR")
                if (
                    response_norm in {"really", "wait seriously"}
                    and not is_surprising_user_text(user_text)
                ):
                    failures.append("BAD_STANDALONE_REACTION")
                if new_provider_calls:
                    if checking_phrase_count(response) > 1:
                        failures.append("DUPLICATE_CHECK_ACK")
                elif effective_mode == "none" and is_only_checking_acknowledgement(
                    response
                ):
                    failures.append("STATIC_CHECKING_ONLY")
                if scenario_name in {"casual", "affection", "anti_parrot"} and (
                    any(phrase in response.casefold() for phrase in _HELPDESK)
                    or _HELPDESK_SELF_POSITIONING.search(response)
                ):
                    failures.append("HELPDESK_FALLBACK")
                if _FALSE_BIOLOGICAL_SELF_CLAIM.search(response):
                    failures.append("FALSE_HUMAN_BIOLOGY")
                if looks_truncated(response, grounded=grounded):
                    failures.append("OBVIOUS_TRUNCATION")

                successful_search = any(
                    int(call["result_count"]) > 0 for call in new_provider_calls
                )
                retrieval_failed_in_response = RETRIEVAL_FAILURE_TEXT in response.casefold()
                if successful_search and retrieval_failed_in_response:
                    failures.append("VALID_RESULTS_NOT_USED")
                elif new_provider_calls and not successful_search:
                    warnings.append("LIVE_RETRIEVAL_UNAVAILABLE")
                if any(call["error"] for call in new_provider_calls):
                    warnings.append("LIVE_RETRIEVAL_ERROR")
                if (
                    last_retrieval_succeeded is False
                    and _REFERENTIAL_FOLLOWUP.search(user_text)
                    and not new_provider_calls
                ):
                    failures.append("FAILED_RETRIEVAL_FOLLOWUP_UNGROUNDED")
                if (
                    scenario_name == "failed_retrieval_followup"
                    and generation == 2
                    and (
                        new_llm_calls
                        or RETRIEVAL_FAILURE_TEXT not in response.casefold()
                    )
                ):
                    failures.append("FAILED_RETRIEVAL_FOLLOWUP_UNGROUNDED")
                if new_provider_calls:
                    last_retrieval_succeeded = successful_search

                normalized_user = normalize(user_text)
                if normalized_user in CHECK_AGAIN:
                    if not new_provider_calls:
                        failures.append("CHECK_AGAIN_NO_FRESH_SEARCH")
                    elif normalize(str(new_provider_calls[-1]["query"])) in CHECK_AGAIN:
                        failures.append("LITERAL_CHECK_AGAIN_SEARCH")
                if scenario_name == "topic_switch" and generation == 3:
                    query_norm = normalize(effective_query or "")
                    if "sports" not in query_norm or "nepal" in query_norm:
                        failures.append("SESSION_TOPIC_CONTAMINATION")
                if scenario_name == "sports" and generation == 2:
                    query_norm = normalize(effective_query or "")
                    if not new_provider_calls or "football" not in query_norm:
                        failures.append("REFINEMENT_REUSED_OLD_REFERENCES")

                latest_llm = new_llm_calls[-1] if new_llm_calls else None
                first_token_ms = latest_llm["first_token_ms"] if latest_llm else None
                llm_total_ms = latest_llm["total_ms"] if latest_llm else None
                token_budget = latest_llm["max_tokens"] if latest_llm else None
                grounded_first_token_ms = first_token_ms if grounded else None
                retrieval_to_grounded_ms = None
                if grounded and new_provider_calls and latest_llm is not None:
                    retrieval_completed = new_provider_calls[-1].get(
                        "completed_monotonic"
                    )
                    grounded_first = latest_llm.get("first_token_monotonic")
                    if isinstance(retrieval_completed, (int, float)) and isinstance(
                        grounded_first, (int, float)
                    ):
                        retrieval_to_grounded_ms = max(
                            0.0, (grounded_first - retrieval_completed) * 1000
                        )
                if isinstance(first_token_ms, (int, float)) and first_token_ms > 2500:
                    warnings.append("LLM_FIRST_TOKEN_SLOW")
                if (
                    first_delta_ms is not None
                    and effective_mode == "none"
                    and first_delta_ms > 2000
                ):
                    warnings.append("CASUAL_FIRST_RESPONSE_SLOW")

                failures = list(dict.fromkeys(failures))
                warnings = list(dict.fromkeys(warnings))
                fail_count += len(failures)
                warn_count += len(warnings)
                status = "FAIL" if failures else "WARN" if warnings else "PASS"

                print(f"\n[{case_number:02d}] {scenario_name} generation={generation}")
                print(f"USER : {user_text}")
                print(f"AANYA: {response or '<EMPTY>'}")
                print(
                    f"MODE : {effective_mode} query={effective_query!r} "
                    f"results={sum(int(c['result_count']) for c in new_provider_calls)}"
                )
                print(
                    f"TIME : first_delta={first_delta_ms:.1f}ms "
                    if first_delta_ms is not None
                    else "TIME : first_delta=NA ",
                    end="",
                )
                print(
                    f"llm_first={first_token_ms:.1f}ms "
                    if isinstance(first_token_ms, (int, float))
                    else "llm_first=NA ",
                    end="",
                )
                print(
                    f"llm_total={llm_total_ms:.1f}ms "
                    if isinstance(llm_total_ms, (int, float))
                    else "llm_total=NA ",
                    end="",
                )
                print(f"overall={total_ms:.1f}ms budget={token_budget}")
                for index, call in enumerate(new_provider_calls, 1):
                    print(
                        f"WEB{index}: query={call['query']!r} "
                        f"results={call['result_count']} "
                        f"time={float(call['elapsed_ms']):.1f}ms "
                        f"error={call['error']}"
                    )
                if failures:
                    print("FAIL : " + ", ".join(failures))
                if warnings:
                    print("WARN : " + ", ".join(warnings))
                print(f"RESULT: {status}")

                report.append(
                    {
                        "case": case_number,
                        "scenario": scenario_name,
                        "generation": generation,
                        "user": user_text,
                        "aanya": response,
                        "retrieval_mode": effective_mode,
                        "effective_retrieval_query": effective_query,
                        "retrieval_result_count": sum(
                            int(call["result_count"]) for call in new_provider_calls
                        ),
                        "retrieval_error": next(
                            (
                                str(call["error"])
                                for call in reversed(new_provider_calls)
                                if call["error"]
                            ),
                            None,
                        ),
                        "retrieval_references_count": sum(
                            int(call["result_count"]) for call in new_provider_calls
                        ),
                        "retrieval_latency_ms": sum(
                            float(call["elapsed_ms"]) for call in new_provider_calls
                        )
                        if new_provider_calls
                        else None,
                        "llm_first_token_ms": first_token_ms,
                        "llm_total_ms": llm_total_ms,
                        "llm_token_budget": token_budget,
                        "grounded_first_token_ms": grounded_first_token_ms,
                        "retrieval_complete_to_grounded_first_token_ms": (
                            retrieval_to_grounded_ms
                        ),
                        "overall_first_delta_ms": first_delta_ms,
                        "overall_total_ms": total_ms,
                        "retrieval_calls": new_provider_calls,
                        "llm_calls": new_llm_calls,
                        "failures": failures,
                        "warnings": warnings,
                        "runtime_error": runtime_error,
                    }
                )
    finally:
        await model.close()

    REPORT_DIRECTORY.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at_epoch": time.time(),
        "cases": len(report),
        "failure_count": fail_count,
        "warning_count": warn_count,
        "results": report,
    }
    encoded = json.dumps(payload, indent=2, ensure_ascii=False)
    timestamped = REPORT_DIRECTORY / (
        "aanya-conversation-regression-" + time.strftime("%Y%m%d-%H%M%S") + ".json"
    )
    LATEST_REPORT_PATH.write_text(encoded, encoding="utf-8")
    timestamped.write_text(encoded, encoding="utf-8")

    deltas = [
        float(item["overall_first_delta_ms"])
        for item in report
        if isinstance(item["overall_first_delta_ms"], (int, float))
    ]
    print(f"\n{'=' * 78}\nSUMMARY\n{'=' * 78}")
    print(f"Cases: {len(report)} Failures: {fail_count} Warnings: {warn_count}")
    if deltas:
        print(f"Median first delta: {statistics.median(deltas):.1f}ms")
    print(f"Report: {timestamped}")
    print("=" * 78)
    return 1 if fail_count else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
