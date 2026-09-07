"""Persistent llama.cpp server adapter for realtime token streaming."""

from __future__ import annotations

import asyncio
import json
import os
import threading
from contextlib import suppress
from dataclasses import dataclass
from typing import AsyncIterator, Mapping
from urllib.parse import urlsplit

import httpx


LLAMA_SERVER_URL_ENVIRONMENT = "AIRA_LLAMA_SERVER_URL"
DEFAULT_LLAMA_SERVER_URL = "http://127.0.0.1:8767"


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

    async def stream(
        self,
        transcript: str,
        cancel_event: threading.Event,
    ) -> AsyncIterator[str]:
        if not isinstance(transcript, str) or not transcript.strip():
            raise ValueError("Realtime LLM transcript must be non-empty.")

        if cancel_event.is_set():
            return

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
            "max_tokens": self._max_tokens,
            "stream": True,
        }

        timeout = httpx.Timeout(
            connect=self._config.connect_timeout_seconds,
            read=None,
            write=self._config.write_timeout_seconds,
            pool=self._config.connect_timeout_seconds,
        )

        yielded_text = False

        async with httpx.AsyncClient(
            timeout=timeout,
            trust_env=False,
        ) as client:
            async with client.stream(
                "POST",
                self._config.chat_completions_url,
                json=payload,
                headers={"Accept": "text/event-stream"},
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
                        yield content
                finally:
                    reader.cancel()
                    with suppress(asyncio.CancelledError):
                        await reader

        if not cancel_event.is_set() and not yielded_text:
            raise RuntimeError("The local llama-server returned no response text.")
