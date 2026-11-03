"""Offline contract tests: real graph, mocked model HTTP, local tools."""
import json
import os
import unittest
from unittest.mock import patch

import httpx
from app import Config, DemoError, create_app, load_config


class OpenAIAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = create_app(Config(
            llm_backend="openai", model_base_url="http://127.0.0.1:8080/v1/",
            model_name="test-local", search_latency_ms=0, fetch_latency_ms=0,
        ))
        self.runtime = self.app.state.runtime
        self.calls = []
        self.answer = "set recovery code LD-042 to enabled"
        self.model_response = None
        self.model_status = 200
        self.local = httpx.ASGITransport(app=self.app)

        async def route(request):
            if request.url.port == 8080:
                self.calls.append(json.loads(request.content))
                text = self.answer if len(self.calls) % 3 == 0 else "mod042"
                data = self.model_response if self.model_response is not None else {
                    "choices": [{"message": {"content": text}}],
                    "usage": {"prompt_tokens": 11, "completion_tokens": 7},
                }
                self.assertEqual(str(request.url), "http://127.0.0.1:8080/v1/chat/completions")
                return httpx.Response(self.model_status, json=data)
            return await self.local.handle_async_request(request)

        self.runtime.client = httpx.AsyncClient(transport=httpx.MockTransport(route))

    async def asyncTearDown(self):
        await self.runtime.client.aclose()
        await self.local.aclose()
        self.runtime.index.close()

    async def request(self):
        return await self.runtime.client.post(
            "http://127.0.0.1:8765/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "Fix mod042"}]},
        )

    async def test_three_model_calls_leave_tools_local_and_sum_usage(self):
        response = await self.request()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.calls), 3)
        for body in self.calls:
            self.assertEqual(body["model"], "test-local")
            self.assertEqual((body["temperature"], body["seed"], body["max_tokens"]), (0, 0, 256))
            self.assertFalse(body["stream"])
        self.assertIn("code in", self.calls[0]["messages"][0]["content"])
        self.assertIn("code to search", self.calls[1]["messages"][0]["content"])
        self.assertIn("supplied support note", self.calls[2]["messages"][0]["content"])
        self.assertIn("set recovery code LD-042 to enabled", self.calls[2]["messages"][1]["content"])
        self.assertIn("[RESOLVED kb-42]", response.text)
        self.assertEqual([response.headers[f"x-demo-{n}-calls"] for n in ("model", "search", "fetch")], ["3", "1", "1"])
        chunks = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: {")]
        self.assertEqual(chunks[-1]["usage"], {"prompt_tokens": 33, "completion_tokens": 21, "total_tokens": 54})

    async def test_ungrounded_writer_is_unresolved(self):
        self.answer = "Try restarting."
        response = await self.request()
        self.assertIn("[UNRESOLVED]", response.text)
        self.assertNotIn("[RESOLVED", response.text)

    async def test_malformed_model_response_returns_503_and_releases_slot(self):
        for data in ({"choices": []}, {"choices": [{"message": {"content": None}}]}, {}):
            with self.subTest(data=data):
                self.model_response = data
                response = await self.request()
                self.assertEqual(response.status_code, 503)
                self.assertEqual(self.runtime.model_in_flight, 0)

    async def test_http_failure_returns_503(self):
        self.model_status = 500
        self.assertEqual((await self.request()).status_code, 503)

    def test_env_settings_and_local_only(self):
        with patch.dict(os.environ, {"LLM_BACKEND": "openai", "DEMO_MODEL_BASE_URL": "http://127.0.0.1:8081/v1", "DEMO_MODEL_NAME": "local-model"}):
            cfg = load_config("search-pool")
        self.assertEqual((cfg.llm_backend, cfg.model_base_url, cfg.model_name), ("openai", "http://127.0.0.1:8081/v1", "local-model"))
        self.assertEqual((cfg.search_workers, cfg.search_latency_ms), (2, 300))
        with self.assertRaises(ValueError):
            Config(model_base_url="https://api.openai.com/v1")
