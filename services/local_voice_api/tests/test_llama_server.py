from __future__ import annotations

import json
import threading
import unittest
from unittest import mock

import httpx

from local_voice_api.llama_server import (
    LlamaServerConfig,
    LlamaServerStreamingLanguageModel,
)


class LlamaServerStreamingLanguageModelTests(unittest.IsolatedAsyncioTestCase):
    async def test_warmup_concurrently_covers_every_bounded_server_slot(
        self,
    ) -> None:
        model = LlamaServerStreamingLanguageModel(
            LlamaServerConfig(),
            system_persona="Truthful local AI.",
            max_tokens=64,
        )
        barrier = threading.Barrier(4)
        warmed: list[int] = []
        warmed_lock = threading.Lock()

        def warm_slot(
            payload: dict[str, object], timeout: httpx.Timeout
        ) -> None:
            self.assertEqual(2, payload["max_tokens"])
            self.assertIsInstance(timeout, httpx.Timeout)
            barrier.wait(timeout=2.0)
            with warmed_lock:
                warmed.append(threading.get_ident())

        with (
            mock.patch.object(model, "_warmup_slot_count", return_value=4),
            mock.patch.object(model, "_warmup_slot", side_effect=warm_slot),
        ):
            with self.assertLogs("local_voice_api.llama_server") as captured:
                model.warmup_inference()

        self.assertEqual(4, len(warmed))
        self.assertEqual(4, len(set(warmed)))
        logs = "\n".join(captured.output)
        self.assertIn("event=llm_warmup_start slot_count=4", logs)
        self.assertIn("event=llm_warmup_complete slot_count=4", logs)

    async def test_warmup_slot_discovery_is_bounded(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual("/props", request.url.path)
            return httpx.Response(200, json={"total_slots": 99})

        model = LlamaServerStreamingLanguageModel(
            LlamaServerConfig(),
            system_persona="Truthful local AI.",
            max_tokens=64,
        )
        client = httpx.Client(transport=httpx.MockTransport(handler))

        with mock.patch(
            "local_voice_api.llama_server.httpx.Client",
            return_value=client,
        ):
            self.assertEqual(4, model._warmup_slot_count())

    async def test_warmup_rejects_stream_without_content(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                text=(
                    'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n'
                    "data: [DONE]\n\n"
                ),
            )

        model = LlamaServerStreamingLanguageModel(
            LlamaServerConfig(),
            system_persona="Truthful local AI.",
            max_tokens=64,
        )
        client = httpx.Client(transport=httpx.MockTransport(handler))

        with mock.patch(
            "local_voice_api.llama_server.httpx.Client",
            return_value=client,
        ):
            with self.assertRaisesRegex(RuntimeError, "returned no stream data"):
                model._warmup_slot(
                    {"model": "local", "stream": True},
                    httpx.Timeout(3.0),
                )

    async def test_per_turn_token_budget_is_sent_without_recreating_client(
        self,
    ) -> None:
        payloads: list[dict[str, object]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            payloads.append(json.loads(request.content))
            body = (
                'data: {"choices":[{"delta":{"content":"Direct "}}]}\n\n'
                'data: {"choices":[{"delta":{"content":"answer."}}]}\n\n'
                'data: [DONE]\n\n'
            )
            return httpx.Response(200, text=body)

        model = LlamaServerStreamingLanguageModel(
            LlamaServerConfig(),
            system_persona="Truthful local AI.",
            max_tokens=64,
        )
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        model._client = client

        try:
            deltas = [
                delta
                async for delta in model.stream_with_token_budget(
                    "Give current news.",
                    threading.Event(),
                    max_tokens=208,
                )
            ]
        finally:
            await model.close()

        self.assertEqual("Direct answer.", "".join(deltas))
        self.assertEqual(208, payloads[0]["max_tokens"])
        self.assertTrue(payloads[0]["stream"])

    async def test_default_stream_preserves_configured_casual_budget(self) -> None:
        payloads: list[dict[str, object]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            payloads.append(json.loads(request.content))
            return httpx.Response(
                200,
                text=(
                    'data: {"choices":[{"delta":{"content":"Hi."}}]}\n\n'
                    'data: [DONE]\n\n'
                ),
            )

        model = LlamaServerStreamingLanguageModel(
            LlamaServerConfig(),
            system_persona="Truthful local AI.",
            max_tokens=64,
        )
        model._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        try:
            response = "".join(
                [
                    delta
                    async for delta in model.stream(
                        "Hello.",
                        threading.Event(),
                    )
                ]
            )
        finally:
            await model.close()

        self.assertEqual("Hi.", response)
        self.assertEqual(64, payloads[0]["max_tokens"])

    async def test_rejects_out_of_range_per_turn_budget(self) -> None:
        model = LlamaServerStreamingLanguageModel(
            LlamaServerConfig(),
            system_persona="Truthful local AI.",
            max_tokens=64,
        )

        with self.assertRaisesRegex(ValueError, "between 1 and 512"):
            await anext(
                model.stream_with_token_budget(
                    "Hello.",
                    threading.Event(),
                    max_tokens=513,
                )
            )

    async def test_sequential_turns_reuse_client_and_emit_content_free_timing(
        self,
    ) -> None:
        request_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal request_count
            request_count += 1
            return httpx.Response(
                200,
                text=(
                    'data: {"choices":[{"delta":{"content":"Okay."}}]}\n\n'
                    'data: [DONE]\n\n'
                ),
            )

        model = LlamaServerStreamingLanguageModel(
            LlamaServerConfig(),
            system_persona="PRIVATE PERSONA",
            max_tokens=64,
        )
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        model._client = client

        try:
            with self.assertLogs("local_voice_api.llama_server") as captured:
                for prompt in ("PRIVATE FIRST PROMPT", "PRIVATE SECOND PROMPT"):
                    response = "".join(
                        [
                            delta
                            async for delta in model.stream(
                                prompt,
                                threading.Event(),
                            )
                        ]
                    )
                    self.assertEqual("Okay.", response)
                self.assertIs(client, await model._async_client())
        finally:
            await model.close()

        logs = "\n".join(captured.output)
        self.assertEqual(2, request_count)
        self.assertIn("event=llm_request_start request_index=1", logs)
        self.assertIn("event=llm_request_start request_index=2", logs)
        self.assertIn("event=llm_first_token", logs)
        self.assertIn("event=llm_request_complete", logs)
        self.assertNotIn("PRIVATE FIRST PROMPT", logs)
        self.assertNotIn("PRIVATE PERSONA", logs)


if __name__ == "__main__":
    unittest.main()
