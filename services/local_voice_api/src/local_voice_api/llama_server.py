"""Persistent llama.cpp server adapter for realtime token streaming."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass
from typing import AsyncIterator, Mapping
from urllib.parse import urlsplit

import httpx


_LOGGER = logging.getLogger(__name__)

LLAMA_SERVER_URL_ENVIRONMENT = "AIRA_LLAMA_SERVER_URL"
DEFAULT_LLAMA_SERVER_URL = "http://127.0.0.1:8767"
MAX_LLAMA_SERVER_WARMUP_SLOTS = 4


@dataclass(frozen=True)
class LlamaServerConfig:
    base_url: str = DEFAULT_LLAMA_SERVER_URL
    connect_timeout_seconds: float = 2.0
    write_timeout_seconds: float = 5.0

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> "LlamaServerConfig":
        source = os.environ if environ is None else environ
        raw_url = source.get(
            LLAMA_SERVER_URL_ENVIRONMENT,
            DEFAULT_LLAMA_SERVER_URL,
        ).strip()

        parsed = urlsplit(raw_url)

        if parsed.scheme != "http":
            raise ValueError("Local llama-server URL must use http://.")

        if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Local llama-server must use a loopback host.")

        if parsed.username or parsed.password:
            raise ValueError("Local llama-server URL must not contain credentials.")

        if parsed.query or parsed.fragment:
            raise ValueError("Local llama-server URL must not contain query or fragment.")

        if parsed.path not in {"", "/"}:
            raise ValueError("Local llama-server URL must not contain a path.")

        return cls(base_url=raw_url.rstrip("/"))

    @property
    def chat_completions_url(self) -> str:
        return f"{self.base_url}/v1/chat/completions"

    @property
    def health_url(self) -> str:
        return f"{self.base_url}/health"

    @property
    def props_url(self) -> str:
        return f"{self.base_url}/props"


class LlamaServerStreamingLanguageModel:
    """Stream text deltas from a persistent local llama.cpp HTTP server."""

    def __init__(
        self,
        config: LlamaServerConfig,
        *,
        system_persona: str,
        max_tokens: int,
    ) -> None:
        if not isinstance(system_persona, str) or not system_persona.strip():
            raise ValueError("System persona must be non-empty.")

        if type(max_tokens) is not int or max_tokens <= 0:
            raise ValueError("LLM maximum token count must be positive.")

        self._config = config
        self._system_persona = system_persona
        self._max_tokens = max_tokens
        self._client: httpx.AsyncClient | None = None
        self._client_lock = asyncio.Lock()
        self._request_count = 0

    def check_ready(self) -> None:
        timeout = httpx.Timeout(
            3.0,
            connect=self._config.connect_timeout_seconds,
        )

        with httpx.Client(
            timeout=timeout,
            trust_env=False,
        ) as client:
            response = client.get(self._config.health_url)
            response.raise_for_status()

            try:
                payload = response.json()
            except ValueError as error:
                raise RuntimeError(
                    "llama-server health endpoint returned invalid JSON."
                ) from error

            if payload.get("status") != "ok":
                raise RuntimeError(
                    f"llama-server is not ready: {payload!r}"
                )

    def warmup_inference(self) -> None:
        """Warm each reported inference slot with a bounded completion."""

        started = time.perf_counter()
        slot_count = self._warmup_slot_count()
        _LOGGER.info(
            "[AIRA WARMUP] event=llm_warmup_start slot_count=%d",
            slot_count,
        )
        payload = {
            "model": "local",
            "messages": [
                {"role": "system", "content": self._system_persona},
                {
                    "role": "user",
                    "content": "Synthetic readiness check. Reply with: ready",
                },
            ],
            "max_tokens": 2,
            "stream": True,
        }
        timeout = httpx.Timeout(
            connect=self._config.connect_timeout_seconds,
            read=30.0,
            write=self._config.write_timeout_seconds,
            pool=self._config.connect_timeout_seconds,
        )

        if slot_count == 1:
            self._warmup_slot(payload, timeout)
        else:
            # Concurrent requests occupy distinct llama-server slots. Warming only
            # one slot leaves a real user's first request free to select a cold one.
            with ThreadPoolExecutor(
                max_workers=slot_count,
                thread_name_prefix="aira-llama-warmup",
            ) as executor:
                futures = [
                    executor.submit(self._warmup_slot, payload, timeout)
                    for _ in range(slot_count)
                ]
                for future in futures:
                    future.result()

        _LOGGER.info(
            "[AIRA WARMUP] event=llm_warmup_complete slot_count=%d elapsed_ms=%.3f",
            slot_count,
            (time.perf_counter() - started) * 1000,
        )

    def _warmup_slot_count(self) -> int:
        timeout = httpx.Timeout(
            3.0,
            connect=self._config.connect_timeout_seconds,
        )
        try:
            with httpx.Client(timeout=timeout, trust_env=False) as client:
                response = client.get(self._config.props_url)
                response.raise_for_status()
                total_slots = response.json().get("total_slots")
        except (httpx.HTTPError, ValueError, AttributeError):
            _LOGGER.warning(
                "[AIRA WARMUP] event=llm_slot_discovery_failed fallback_slots=1"
            )
            return 1

        if type(total_slots) is not int or total_slots <= 0:
            _LOGGER.warning(
                "[AIRA WARMUP] event=llm_slot_discovery_invalid fallback_slots=1"
            )
            return 1
        return min(total_slots, MAX_LLAMA_SERVER_WARMUP_SLOTS)

    def _warmup_slot(
        self,
        payload: dict[str, object],
        timeout: httpx.Timeout,
    ) -> None:
        received = False
        with httpx.Client(timeout=timeout, trust_env=False) as client:
            with client.stream(
                "POST",
                self._config.chat_completions_url,
                json=payload,
                headers={"Accept": "text/event-stream"},
            ) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    rendered = line.strip()
                    if not rendered.startswith("data:"):
                        continue
                    data = rendered[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    try:
                        event = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    choices = event.get("choices") or []
                    if choices:
                        content = (choices[0].get("delta") or {}).get("content")
                        received = received or bool(content)
        if not received:
            raise RuntimeError("llama-server synthetic warmup returned no stream data.")

    async def stream(
        self,
        transcript: str,
        cancel_event: threading.Event,
    ) -> AsyncIterator[str]:
        async for delta in self.stream_with_token_budget(
            transcript,
            cancel_event,
            max_tokens=self._max_tokens,
        ):
            yield delta

    async def stream_with_token_budget(
        self,
        transcript: str,
        cancel_event: threading.Event,
        *,
        max_tokens: int,
    ) -> AsyncIterator[str]:
        if not isinstance(transcript, str) or not transcript.strip():
            raise ValueError("Realtime LLM transcript must be non-empty.")

        if type(max_tokens) is not int or not 1 <= max_tokens <= 512:
            raise ValueError("Realtime LLM token budget must be between 1 and 512.")

        if cancel_event.is_set():
            return

        self._request_count += 1
        request_index = self._request_count
        request_started = time.perf_counter()
        _LOGGER.info(
            "[AIRA LLM] event=llm_request_start request_index=%d "
            "prompt_characters=%d max_tokens=%d",
            request_index,
            len(transcript),
            max_tokens,
        )

        payload = {
            "model": "local",
            "messages": [
                {
                    "role": "system",
                    "content": self._system_persona,
                },
                {
                    "role": "user",
                    "content": transcript,
                },
            ],
            "max_tokens": max_tokens,
            "stream": True,
        }

        timeout = httpx.Timeout(
            connect=self._config.connect_timeout_seconds,
            read=None,
            write=self._config.write_timeout_seconds,
            pool=self._config.connect_timeout_seconds,
        )

        yielded_text = False
        first_content_at: float | None = None

        client = await self._async_client()
        async with client.stream(
            "POST",
            self._config.chat_completions_url,
            json=payload,
            headers={"Accept": "text/event-stream"},
            timeout=timeout,
        ) as response:
            response.raise_for_status()

            queue: asyncio.Queue[object] = asyncio.Queue()
            finished = object()

            async def read_stream() -> None:
                try:
                    async for line in response.aiter_lines():
                        await queue.put(line)
                except asyncio.CancelledError:
                    raise
                except BaseException as error:
                    await queue.put(error)
                finally:
                    await queue.put(finished)

            reader = asyncio.create_task(read_stream())

            try:
                while True:
                    if cancel_event.is_set():
                        return

                    try:
                        item = await asyncio.wait_for(
                            queue.get(),
                            timeout=0.05,
                        )
                    except TimeoutError:
                        continue

                    if item is finished:
                        break

                    if isinstance(item, BaseException):
                        raise item

                    if not isinstance(item, str):
                        continue

                    line = item.strip()

                    if not line.startswith("data:"):
                        continue

                    data = line[5:].strip()

                    if not data:
                        continue

                    if data == "[DONE]":
                        break

                    try:
                        event = json.loads(data)
                    except json.JSONDecodeError as error:
                        raise RuntimeError(
                            "llama-server returned malformed SSE JSON."
                        ) from error

                    choices = event.get("choices") or []

                    if not choices:
                        continue

                    delta = choices[0].get("delta") or {}
                    content = delta.get("content")

                    if content is None:
                        continue

                    if not isinstance(content, str):
                        raise RuntimeError(
                            "llama-server returned a non-text delta."
                        )

                    if not content:
                        continue

                    yielded_text = True
                    if first_content_at is None:
                        first_content_at = time.perf_counter()
                        _LOGGER.info(
                            "[AIRA LLM] event=llm_first_token request_index=%d "
                            "elapsed_ms=%.3f",
                            request_index,
                            (first_content_at - request_started) * 1000,
                        )
                    yield content
            finally:
                reader.cancel()
                with suppress(asyncio.CancelledError):
                    await reader

        _LOGGER.info(
            "[AIRA LLM] event=llm_request_complete request_index=%d "
            "elapsed_ms=%.3f produced_text=%s cancelled=%s",
            request_index,
            (time.perf_counter() - request_started) * 1000,
            str(yielded_text).lower(),
            str(cancel_event.is_set()).lower(),
        )
        if not cancel_event.is_set() and not yielded_text:
            raise RuntimeError("The local llama-server returned no response text.")

    async def prepare(self) -> None:
        """Open and retain the persistent loopback HTTP connection."""

        timeout = httpx.Timeout(
            3.0,
            connect=self._config.connect_timeout_seconds,
        )
        client = await self._async_client()
        response = await client.get(
            self._config.health_url,
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("status") != "ok":
            raise RuntimeError("llama-server is not ready.")

    async def close(self) -> None:
        async with self._client_lock:
            client = self._client
            self._client = None
        if client is not None:
            await client.aclose()

    async def _async_client(self) -> httpx.AsyncClient:
        async with self._client_lock:
            if self._client is None:
                self._client = httpx.AsyncClient(trust_env=False)
                _LOGGER.info("[AIRA LLM] event=async_http_client_created")
            return self._client
