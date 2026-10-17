"""Focused checks for the planted tools and three-node LangGraph path."""

import asyncio
import json
import unittest

import httpx
from app import Config, DemoError, ModelResult, create_app, load_config


class DemoTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = create_app(Config(
            model_base_ms=0, model_per_token_ms=0, search_latency_ms=0,
            fetch_latency_ms=0,
        ))
        self.runtime = self.app.state.runtime
        self.runtime.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app))

    async def asyncTearDown(self):
        await self.runtime.client.aclose()
        self.runtime.index.close()

    async def test_search_and_http_fetch_use_ground_truth(self):
        article_id = await self.runtime.search("mod042", "one")
        self.assertEqual(article_id, "kb-42")
        article = await self.runtime.fetch(article_id, "one")
        self.assertEqual(article["key_fact"], "set recovery code LD-042 to enabled")
        self.assertEqual(self.runtime.stats["search_calls"], 1)
        self.assertEqual(self.runtime.stats["fetch_calls"], 1)

    async def test_langgraph_has_three_named_nodes_and_correct_answer(self):
        self.assertTrue({"planner", "researcher", "writer"}.issubset(
            self.runtime.graph.get_graph().nodes,
        ))
        state = await self.runtime.graph.ainvoke({
            "question": "How do I restore the LanternDesk mod042 status panel?",
            "request_key": "s1", "input_tokens": 0, "output_tokens": 0,
            "model_calls": 0, "search_calls": 0, "fetch_calls": 0,
        })
        self.assertIn("set recovery code LD-042 to enabled", state["answer"])
        self.assertIn("[RESOLVED kb-42]", state["answer"])
        self.assertEqual((state["model_calls"], state["search_calls"],
                          state["fetch_calls"]), (3, 1, 1))
        self.assertGreater(state["input_tokens"], 0)
        self.assertGreater(state["output_tokens"], 0)

    async def test_search_pool_capacity_blocks_until_slot_is_free(self):
        await self.runtime.search_workers.acquire()
        try:
            task = asyncio.create_task(self.runtime.search("mod042", "one"))
            await asyncio.sleep(0)
            self.assertFalse(task.done())
        finally:
            self.runtime.search_workers.release()
        self.assertEqual(await task, "kb-42")

    async def test_sim_errors_and_fetch_failure_are_controllable(self):
        self.runtime.config = Config(model_error_rate=1, model_base_ms=0)
        with self.assertRaises(DemoError):
            await self.runtime.model("planner", "mod042", "one")
        self.runtime.config = Config(fetch_error_rate=1, model_base_ms=0,
                                     model_per_token_ms=0, search_latency_ms=0,
                                     fetch_latency_ms=0)
        self.assertIsNone(await self.runtime.fetch("kb-42", "one"))
        self.assertEqual(self.runtime.stats["model_errors"], 1)
        self.assertEqual(self.runtime.stats["fetch_errors"], 1)

    async def test_fetch_timeout_includes_rate_limit_queue(self):
        self.runtime.config = Config(
            fetch_rate_per_s=1, fetch_timeout_s=0.03,
            model_base_ms=0, model_per_token_ms=0,
            search_latency_ms=0, fetch_latency_ms=0,
        )
        self.assertIsNotNone(await self.runtime.fetch("kb-42", "first"))
        started = asyncio.get_running_loop().time()
        self.assertIsNone(await self.runtime.fetch("kb-42", "second"))
        self.assertLess(asyncio.get_running_loop().time() - started, 0.2)
        self.assertEqual(self.runtime.stats["fetch_errors"], 1)

    async def test_front_door_streams_usage_and_accepts_multiturn_traceparent(self):
        response = await self.runtime.client.post(
            "http://127.0.0.1:8765/v1/chat/completions",
            headers={"x-kneepoint-session": "s1", "traceparent": "00-" + "a" * 32
                     + "-" + "b" * 16 + "-01"},
            json={"model": "lantern-desk", "stream": True,
                  "messages": [{"role": "user", "content": "Earlier question"},
                               {"role": "assistant", "content": "Earlier reply"},
                               {"role": "user", "content": "Fix mod042 status panel"}]},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["x-demo-model-calls"], "3")
        self.assertEqual(response.headers["x-demo-search-calls"], "1")
        self.assertEqual(response.headers["x-demo-fetch-calls"], "1")
        chunks = [json.loads(line[6:]) for line in response.text.splitlines()
                  if line.startswith("data: {")]
        self.assertIn("[RESOLVED kb-42]", chunks[1]["choices"][0]["delta"]["content"])
        self.assertGreater(chunks[-1]["usage"]["total_tokens"], 0)

    async def test_followup_uses_last_relevant_module_and_can_switch(self):
        async def answer(messages):
            response = await self.runtime.client.post(
                "http://127.0.0.1:8765/v1/chat/completions",
                json={"model": "lantern-desk", "stream": True, "messages": messages},
            )
            chunks = [json.loads(line[6:]) for line in response.text.splitlines()
                      if line.startswith("data: {")]
            return chunks[1]["choices"][0]["delta"]["content"]

        followup = await answer([
            {"role": "user", "content": "How do I restore mod042?"},
            {"role": "assistant", "content": "Let me check mod042."},
            {"role": "user", "content": "What exact setting fixes it?"},
        ])
        self.assertIn("[RESOLVED kb-42]", followup)
        switched = await answer([
            {"role": "user", "content": "How do I restore mod041?"},
            {"role": "assistant", "content": "I found mod041."},
            {"role": "user", "content": "Actually, how do I restore mod042?"},
        ])
        self.assertIn("[RESOLVED kb-42]", switched)
        self.assertNotIn("[RESOLVED kb-41]", switched)

    async def test_ollama_writer_cannot_claim_resolution_without_exact_fact(self):
        article = self.runtime.articles["kb-42"]
        self.runtime.config = Config(llm_backend="ollama")

        async def ungrounded(*_args):
            return ModelResult("Try a different setting.", 3, 4)

        self.runtime.model = ungrounded
        state = await self.runtime.writer({
            "question": "Fix mod042", "request_key": "s1", "article": article,
            "input_tokens": 0, "output_tokens": 0, "model_calls": 0,
        })
        self.assertIn("[UNRESOLVED]", state["answer"])
        self.assertNotIn("[RESOLVED", state["answer"])


class ConfigTests(unittest.TestCase):
    def test_named_presets_and_local_only_endpoints(self):
        self.assertEqual(load_config("search-pool").search_workers, 2)
        self.assertEqual(load_config("fetch-ratelimit").fetch_rate_per_s, 7)
        with self.assertRaises(ValueError):
            Config(ollama_url="https://api.example.com")


if __name__ == "__main__":
    unittest.main()
