"""Focused checks for the planted tools and three-node LangGraph path."""

import asyncio
import json
import unittest

import httpx
from app import Config, DemoError, ModelResult, TraceStreamingResponse, create_app, load_config
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from telemetry import Telemetry


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


class CaptureFetch(httpx.AsyncBaseTransport):
    def __init__(self, app):
        self.inner = httpx.ASGITransport(app=app)
        self.headers = []

    async def handle_async_request(self, request):
        if request.url.path.startswith("/documents/"):
            self.headers.append(dict(request.headers))
        return await self.inner.handle_async_request(request)

    async def aclose(self):
        await self.inner.aclose()


class TraceTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_slot_and_fetch_limiter_wait_spans(self):
        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        app = create_app(Config(model_slots=1, model_base_ms=50,
                                model_per_token_ms=0, search_latency_ms=0,
                                fetch_rate_per_s=20, fetch_latency_ms=0),
                         telemetry=Telemetry(enabled=True, provider=provider))
        runtime = app.state.runtime
        runtime.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app))
        try:
            await asyncio.gather(runtime.model("planner", "mod042", "one"),
                                 runtime.model("planner", "mod042", "two"))
            self.assertIsNotNone(await runtime.fetch("kb-42", "one"))
            self.assertIsNotNone(await runtime.fetch("kb-42", "two"))
            spans = exporter.get_finished_spans()
            model = [span for span in spans if span.name == "wait model slot"]
            fetch = [span for span in spans if span.name == "wait fetch rate limiter"]
            self.assertEqual((len(model), len(fetch)), (2, 2))
            self.assertGreater(max((span.end_time - span.start_time) / 1e6
                                   for span in model), 30)
            self.assertGreater(max((span.end_time - span.start_time) / 1e6
                                   for span in fetch), 30)
            self.assertTrue(all(span.attributes["demo.pool.capacity"] == 1
                                for span in model))
            self.assertTrue(all(span.attributes["demo.pool.capacity"] == 20
                                for span in fetch))
        finally:
            await runtime.client.aclose()
            runtime.index.close()
            provider.shutdown()

    async def test_sse_disconnect_ends_server_span(self):
        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        span = provider.get_tracer("test").start_span("POST /v1/chat/completions")

        async def body():
            yield b"data: test\n\n"

        async def send(message):
            if message["type"] == "http.response.body":
                raise RuntimeError("client disconnected")

        async def receive():
            return {"type": "http.disconnect"}

        response = TraceStreamingResponse(body(), trace_span=span)
        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
                 "method": "POST", "path": "/v1/chat/completions", "headers": []}
        try:
            with self.assertRaises(RuntimeError):
                await response(scope, receive, send)
            finished = exporter.get_finished_spans()
            self.assertEqual(len(finished), 1)
            self.assertEqual(finished[0].attributes["error.type"], "RuntimeError")
        finally:
            provider.shutdown()

    async def test_disabled_tracing_still_forwards_incoming_context(self):
        app = create_app(Config(model_base_ms=0, model_per_token_ms=0,
                                search_latency_ms=0, fetch_latency_ms=0),
                         telemetry=Telemetry(enabled=False))
        runtime = app.state.runtime
        transport = CaptureFetch(app)
        runtime.client = httpx.AsyncClient(transport=transport)
        traceparent = f"00-{'a' * 32}-{'b' * 16}-01"
        try:
            response = await runtime.client.post(
                "http://127.0.0.1:8765/v1/chat/completions",
                headers={"traceparent": traceparent, "baggage": "kneepoint.level=7"},
                json={"messages": [{"role": "user", "content": "Fix mod042"}]},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(transport.headers[0]["traceparent"], traceparent)
            self.assertIn("kneepoint.level=7", transport.headers[0]["baggage"])
        finally:
            await runtime.client.aclose()
            runtime.index.close()

    async def test_search_wait_is_separate_from_service_under_contention(self):
        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        app = create_app(Config(model_base_ms=0, model_per_token_ms=0,
                                search_workers=1, search_latency_ms=50,
                                fetch_latency_ms=0),
                         telemetry=Telemetry(enabled=True, provider=provider))
        runtime = app.state.runtime
        runtime.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app))
        try:
            async def ask(digit):
                return await runtime.client.post(
                    "http://127.0.0.1:8765/v1/chat/completions",
                    headers={"traceparent": f"00-{str(digit) * 32}-{'b' * 16}-01"},
                    json={"messages": [{"role": "user", "content": "Fix mod042"}]},
                )

            responses = await asyncio.gather(ask(1), ask(2))
            self.assertTrue(all(response.status_code == 200 for response in responses))
            search = [span for span in exporter.get_finished_spans()
                      if span.attributes.get("demo.step.name") == "search"]
            self.assertEqual(len(search), 2)
            self.assertGreater(max(span.attributes["demo.queue_wait_ms"]
                                   for span in search), 30)
            self.assertTrue(all(span.attributes["demo.service_time_ms"] >= 45
                                for span in search))
            self.assertTrue(all(span.attributes["demo.pool.capacity"] == 1
                                for span in search))
        finally:
            await runtime.client.aclose()
            runtime.index.close()
            provider.shutdown()

    async def test_incoming_trace_covers_nodes_waits_and_fetch_http_hop(self):
        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        app = create_app(Config(model_base_ms=0, model_per_token_ms=0,
                                search_latency_ms=0, fetch_latency_ms=0),
                         telemetry=Telemetry(enabled=True, provider=provider))
        runtime = app.state.runtime
        transport = CaptureFetch(app)
        runtime.client = httpx.AsyncClient(transport=transport)
        trace_id = "a" * 32
        parent_id = "b" * 16
        try:
            response = await runtime.client.post(
                "http://127.0.0.1:8765/v1/chat/completions",
                headers={"traceparent": f"00-{trace_id}-{parent_id}-01",
                         "baggage": "kneepoint.run=run-1,kneepoint.level=7"},
                json={"messages": [{"role": "user", "content": "Fix mod042"}]},
            )
            self.assertEqual(response.status_code, 200)
            spans = exporter.get_finished_spans()
            self.assertEqual(len({s.context.trace_id for s in spans}), 1)
            self.assertEqual(f"{spans[0].context.trace_id:032x}", trace_id)
            by_name = {s.name: s for s in spans}
            server = by_name["POST /v1/chat/completions"]
            self.assertEqual(f"{server.parent.span_id:016x}", parent_id)
            self.assertEqual(server.attributes["baggage.kneepoint.run"], "run-1")
            self.assertEqual(server.attributes["baggage.kneepoint.level"], "7")
            for node in ("planner", "researcher", "writer"):
                agent = by_name[f"invoke_agent {node}"]
                self.assertEqual(agent.parent.span_id, server.context.span_id)
                self.assertEqual(agent.attributes["gen_ai.operation.name"], "invoke_agent")
                self.assertEqual(agent.attributes["gen_ai.agent.name"], node)
                self.assertEqual(agent.attributes["gen_ai.provider.name"], "langgraph")
            chats = [s for s in spans if s.attributes.get("gen_ai.operation.name") == "chat"]
            self.assertEqual(len(chats), 3)
            for chat in chats:
                self.assertIn(chat.parent.span_id,
                              {by_name[f"invoke_agent {n}"].context.span_id
                               for n in ("planner", "researcher", "writer")})
                self.assertGreater(chat.attributes["gen_ai.usage.input_tokens"], 0)
                self.assertGreater(chat.attributes["gen_ai.usage.output_tokens"], 0)
                self.assertIn("demo.service_time_ms", chat.attributes)
                self.assertIn("demo.queue_wait_ms", chat.attributes)
            for tool in ("search", "fetch"):
                span = by_name[f"execute_tool {tool}"]
                self.assertEqual(span.attributes["gen_ai.tool.name"], tool)
                self.assertEqual(span.parent.span_id,
                                 by_name["invoke_agent researcher"].context.span_id)
                self.assertIn("demo.service_time_ms", span.attributes)
                self.assertGreaterEqual(span.attributes["demo.pool.in_flight"], 1)
            waits = [s for s in spans if s.name.startswith("wait ")]
            self.assertEqual(len(waits), 5)
            self.assertTrue(all("demo.pool.capacity" in s.attributes for s in waits))
            http_spans = [s for s in spans if s.name == "GET /documents/{article_id}"]
            self.assertEqual(len(http_spans), 2)
            client = next(s for s in http_spans if s.kind.name == "CLIENT")
            document_server = next(s for s in http_spans if s.kind.name == "SERVER")
            self.assertEqual(client.parent.span_id, by_name["execute_tool fetch"].context.span_id)
            self.assertEqual(document_server.parent.span_id, client.context.span_id)
            self.assertGreaterEqual(server.end_time,
                                    max(span.end_time for span in spans if span is not server))
            self.assertEqual(len(transport.headers), 1)
            self.assertIn("kneepoint.run=run-1", transport.headers[0]["baggage"])
            self.assertIn(trace_id, transport.headers[0]["traceparent"])
            invalid = await runtime.client.post(
                "http://127.0.0.1:8765/v1/chat/completions",
                headers={"traceparent": f"00-{trace_id}-{parent_id}-01"},
                json={"messages": []},
            )
            self.assertEqual(invalid.status_code, 400)
            self.assertEqual(exporter.get_finished_spans()[-1].attributes[
                "http.response.status_code"], 400)
        finally:
            await runtime.client.aclose()
            runtime.index.close()
            provider.shutdown()


if __name__ == "__main__":
    unittest.main()
