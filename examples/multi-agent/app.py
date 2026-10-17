"""LanternDesk: a controllable LangGraph support agent for load experiments.

Slice A has explicit graph nodes and local tools. OTel spans and traceparent
propagation inside the agent belong to slice B.
"""

import asyncio
import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import TypedDict
from urllib.parse import urlparse

import httpx
import yaml
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from langgraph.graph import END, START, StateGraph

ROOT = Path(__file__).resolve().parent
MODULE = re.compile(r"\bmod\d{3}\b", re.IGNORECASE)


def _loopback(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError(f"only local HTTP endpoints are allowed: {url}")
    return url.rstrip("/")


@dataclass(frozen=True)
class Config:
    profile: str = "none"
    model_slots: int = 32
    model_base_ms: float = 25
    model_per_token_ms: float = 0.05
    model_error_rate: float = 0
    search_workers: int = 16
    search_latency_ms: float = 5
    search_error_rate: float = 0
    fetch_rate_per_s: float = 0
    fetch_latency_ms: float = 5
    fetch_error_rate: float = 0
    model_timeout_s: float = 10
    search_timeout_s: float = 10
    fetch_timeout_s: float = 10
    local_base_url: str = "http://127.0.0.1:8765"
    llm_backend: str = "sim"
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen2.5:3b"

    def __post_init__(self) -> None:
        if self.model_slots < 1 or self.search_workers < 1:
            raise ValueError("model_slots and search_workers must be positive")
        for name in ("model_base_ms", "model_per_token_ms", "search_latency_ms",
                     "fetch_latency_ms", "fetch_rate_per_s"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be nonnegative")
        for name in ("model_error_rate", "search_error_rate", "fetch_error_rate"):
            if not 0 <= getattr(self, name) <= 1:
                raise ValueError(f"{name} must be between 0 and 1")
        for name in ("model_timeout_s", "search_timeout_s", "fetch_timeout_s"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.llm_backend not in {"sim", "ollama"}:
            raise ValueError("LLM_BACKEND must be sim or ollama")
        _loopback(self.local_base_url)
        _loopback(self.ollama_url)


def load_config(profile: str | None = None) -> Config:
    chosen = profile or os.getenv("DEMO_PROFILE", "none")
    profiles = yaml.safe_load((ROOT / "profiles.yaml").read_text(encoding="utf-8"))
    if chosen not in profiles:
        raise ValueError(f"unknown DEMO_PROFILE {chosen!r}; choose {', '.join(profiles)}")
    values = {"profile": chosen, **profiles[chosen]}
    defaults = Config()
    for field in fields(Config):
        if field.name == "profile":
            continue
        env_name = "LLM_BACKEND" if field.name == "llm_backend" else (
            "OLLAMA_URL" if field.name == "ollama_url" else (
                "OLLAMA_MODEL" if field.name == "ollama_model" else f"DEMO_{field.name.upper()}")
        )
        raw = os.getenv(env_name)
        if raw is not None:
            default = getattr(defaults, field.name)
            values[field.name] = type(default)(raw)
    return Config(**values)


class DemoError(Exception):
    pass


class AgentState(TypedDict, total=False):
    question: str
    module_hint: str
    request_key: str
    query: str
    article: dict | None
    answer: str
    input_tokens: int
    output_tokens: int
    model_calls: int
    search_calls: int
    fetch_calls: int


@dataclass(frozen=True)
class ModelResult:
    text: str
    input_tokens: int
    output_tokens: int


class Runtime:
    def __init__(self, config: Config, articles: list[dict]) -> None:
        self.config = config
        self.articles = {article["id"]: article for article in articles}
        self.index = sqlite3.connect(":memory:", check_same_thread=False)
        self.index.execute("CREATE VIRTUAL TABLE kb USING fts5(id UNINDEXED, module, body)")
        self.index.executemany(
            "INSERT INTO kb (id, module, body) VALUES (?, ?, ?)",
            ((a["id"], a["module"], a["body"]) for a in articles),
        )
        self.model_slots = asyncio.Semaphore(config.model_slots)
        self.search_workers = asyncio.Semaphore(config.search_workers)
        self.fetch_lock = asyncio.Lock()
        self.next_fetch_at = 0.0
        self.client: httpx.AsyncClient | None = None
        self.stats = {
            "requests": 0, "model_calls": 0, "search_calls": 0, "fetch_calls": 0,
            "model_errors": 0, "search_errors": 0, "fetch_errors": 0,
        }
        graph = StateGraph(AgentState)
        graph.add_node("planner", self.planner)
        graph.add_node("researcher", self.researcher)
        graph.add_node("writer", self.writer)
        graph.add_edge(START, "planner")
        graph.add_edge("planner", "researcher")
        graph.add_edge("researcher", "writer")
        graph.add_edge("writer", END)
        self.graph = graph.compile()

    @staticmethod
    def _fails(rate: float, key: str) -> bool:
        fraction = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big") / 2**64
        return fraction < rate

    async def model(
        self, role: str, prompt: str, key: str, article: dict | None = None,
    ) -> ModelResult:
        self.stats["model_calls"] += 1
        try:
            return await asyncio.wait_for(
                self._model(role, prompt, key, article), self.config.model_timeout_s,
            )
        except (TimeoutError, DemoError):
            self.stats["model_errors"] += 1
            raise

    async def _model(
        self, role: str, prompt: str, key: str, article: dict | None,
    ) -> ModelResult:
        async with self.model_slots:
            if self.config.llm_backend == "ollama":
                if self.client is None:
                    raise DemoError("HTTP client is not ready")
                instructions = {
                    "planner": "Return only the mod### code in this support question.",
                    "researcher": (
                        "Return only the mod### code to search for this support question."
                    ),
                    "writer": (
                        "Answer using only the supplied support note. Copy its key fact "
                        "verbatim, without formatting or paraphrase."
                    ),
                }
                try:
                    response = await self.client.post(
                        f"{self.config.ollama_url}/api/generate",
                        json={"model": self.config.ollama_model,
                              "prompt": f"{instructions[role]}\n{prompt}", "stream": False},
                        timeout=self.config.model_timeout_s,
                    )
                    response.raise_for_status()
                    data = response.json()
                    return ModelResult(
                        str(data["response"]), int(data["prompt_eval_count"]),
                        int(data["eval_count"]),
                    )
                except (httpx.HTTPError, KeyError, ValueError) as exc:
                    raise DemoError(f"ollama {role}: {type(exc).__name__}") from exc
            if self._fails(self.config.model_error_rate, f"{key}:model:{role}"):
                raise DemoError(f"simulated {role} model error")
            match = MODULE.search(prompt)
            if role == "writer":
                text = (
                    f"To restore the LanternDesk {article['module']} status panel, "
                    f"{article['key_fact']}. [RESOLVED {article['id']}]"
                    if article else "I could not verify the support note. [UNRESOLVED]"
                )
            else:
                text = match.group(0).lower() if match else "unknown"
            in_tokens, out_tokens = len(prompt.split()), len(text.split())
            await asyncio.sleep(
                (self.config.model_base_ms + self.config.model_per_token_ms * out_tokens)
                / 1000,
            )
            return ModelResult(text, in_tokens, out_tokens)

    async def search(self, query: str, key: str) -> str | None:
        self.stats["search_calls"] += 1

        async def work() -> str | None:
            async with self.search_workers:
                await asyncio.sleep(self.config.search_latency_ms / 1000)
                if self._fails(self.config.search_error_rate, f"{key}:search"):
                    raise DemoError("simulated search error")
                match = MODULE.search(query)
                if match is None:
                    return None
                row = self.index.execute(
                    "SELECT id FROM kb WHERE kb MATCH ? LIMIT 1", (match.group(0).lower(),),
                ).fetchone()
                return row[0] if row else None

        try:
            return await asyncio.wait_for(work(), self.config.search_timeout_s)
        except (TimeoutError, DemoError):
            self.stats["search_errors"] += 1
            return None

    async def fetch(self, article_id: str, key: str) -> dict | None:
        self.stats["fetch_calls"] += 1
        if self.client is None:
            raise DemoError("HTTP client is not ready")

        async def work() -> dict:
            # Include the rate-limit queue in the fetch timeout. Keeping the
            # wait under the lock avoids reserving a slot for cancelled calls.
            async with self.fetch_lock:
                if self.config.fetch_rate_per_s:
                    delay = max(0.0, self.next_fetch_at - time.monotonic())
                    if delay:
                        await asyncio.sleep(delay)
                    self.next_fetch_at = time.monotonic() + 1 / self.config.fetch_rate_per_s
            response = await self.client.get(
                f"{self.config.local_base_url}/documents/{article_id}",
                timeout=self.config.fetch_timeout_s,
            )
            response.raise_for_status()
            if self._fails(self.config.fetch_error_rate, f"{key}:fetch"):
                raise DemoError("simulated fetch error")
            return response.json()

        try:
            return await asyncio.wait_for(work(), self.config.fetch_timeout_s)
        except (TimeoutError, httpx.HTTPError, DemoError, ValueError):
            self.stats["fetch_errors"] += 1
            return None

    async def planner(self, state: AgentState) -> AgentState:
        prompt = (
            f"Latest user request: {state['question']}\n"
            f"Relevant module from conversation: {state.get('module_hint', 'unknown')}"
        )
        result = await self.model("planner", prompt, state["request_key"])
        return {
            "query": result.text,
            "input_tokens": state["input_tokens"] + result.input_tokens,
            "output_tokens": state["output_tokens"] + result.output_tokens,
            "model_calls": state["model_calls"] + 1,
        }

    async def researcher(self, state: AgentState) -> AgentState:
        result = await self.model(
            "researcher", f"Question: {state['question']}\nPlan: {state['query']}",
            state["request_key"],
        )
        article_id = await self.search(result.text, state["request_key"])
        article = await self.fetch(article_id, state["request_key"]) if article_id else None
        return {
            "article": article,
            "input_tokens": state["input_tokens"] + result.input_tokens,
            "output_tokens": state["output_tokens"] + result.output_tokens,
            "model_calls": state["model_calls"] + 1,
            "search_calls": state["search_calls"] + 1,
            "fetch_calls": state["fetch_calls"] + int(article_id is not None),
        }

    async def writer(self, state: AgentState) -> AgentState:
        article = state.get("article")
        prompt = (
            f"Question: {state['question']}\n"
            f"Support note: {article['body'] if article else 'none'}"
        )
        result = await self.model("writer", prompt, state["request_key"], article)
        if self.config.llm_backend == "ollama":
            # Do not turn a fluent but ungrounded model answer into a resolution.
            answer = (
                f"{result.text.strip()} [RESOLVED {article['id']}]"
                if article and article["key_fact"] in result.text
                else f"{result.text.strip()} [UNRESOLVED]"
            )
        else:
            answer = result.text
        return {
            "answer": answer,
            "input_tokens": state["input_tokens"] + result.input_tokens,
            "output_tokens": state["output_tokens"] + result.output_tokens,
            "model_calls": state["model_calls"] + 1,
        }


def create_app(config: Config | None = None, *, data_dir: Path = ROOT) -> FastAPI:
    # This target never exports telemetry in slice A, even if the caller's
    # shell enables LangSmith tracing for unrelated projects.
    os.environ["LANGSMITH_TRACING"] = "false"
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    config = config or load_config()
    articles = json.loads((data_dir / "articles.json").read_text(encoding="utf-8"))
    runtime = Runtime(config, articles)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        runtime.client = httpx.AsyncClient(trust_env=False)
        try:
            yield
        finally:
            await runtime.client.aclose()
            runtime.index.close()

    app = FastAPI(title="LanternDesk demo agent", docs_url=None, redoc_url=None,
                  lifespan=lifespan)
    app.state.runtime = runtime

    @app.get("/debug/config")
    async def debug_config() -> dict:
        return asdict(runtime.config)

    @app.get("/debug/stats")
    async def debug_stats() -> dict:
        return dict(runtime.stats)

    @app.get("/documents/{article_id}")
    async def get_document(article_id: str):
        await asyncio.sleep(config.fetch_latency_ms / 1000)
        article = runtime.articles.get(article_id)
        return article if article else JSONResponse({"error": "not found"}, status_code=404)

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        body = await request.json()
        messages = body.get("messages") or []
        if not isinstance(messages, list) or not messages:
            return JSONResponse({"error": "messages required"}, status_code=400)
        user_messages = [str(m.get("content", "")) for m in messages if m.get("role") == "user"]
        if not user_messages:
            return JSONResponse({"error": "user message required"}, status_code=400)
        question = user_messages[-1]
        # A follow-up can omit the module. Prefer the latest user's explicit
        # reference, then a prior assistant's, so the current turn can switch
        # modules without inheriting an older one.
        module_hint = next(
            (match.group(0).lower() for message in reversed(messages)
             if (match := MODULE.search(str(message.get("content", ""))))),
            "unknown",
        )
        session = request.headers.get("x-kneepoint-session", "")
        traceparent = request.headers.get("traceparent", "")  # accepted; slice B propagates it
        key = f"{session}:{traceparent}:{question}"
        runtime.stats["requests"] += 1
        try:
            state = await runtime.graph.ainvoke({
                "question": question, "module_hint": module_hint,
                "request_key": key,
                "input_tokens": 0, "output_tokens": 0,
                "model_calls": 0, "search_calls": 0, "fetch_calls": 0,
            })
        except (DemoError, TimeoutError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=503)
        answer = state["answer"]
        completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"

        def chunk(delta: dict, finish: str | None = None) -> dict:
            return {
                "id": completion_id, "object": "chat.completion.chunk",
                "created": int(time.time()), "model": body.get("model", "lantern-desk"),
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }

        async def stream():
            for payload in (
                chunk({"role": "assistant"}),
                chunk({"content": answer}),
                chunk({}, "stop"),
                {"id": completion_id, "object": "chat.completion.chunk",
                 "created": int(time.time()), "model": body.get("model", "lantern-desk"),
                 "choices": [],
                 "usage": {"prompt_tokens": state["input_tokens"],
                           "completion_tokens": state["output_tokens"],
                           "total_tokens": state["input_tokens"] + state["output_tokens"]}},
            ):
                yield f"data: {json.dumps(payload)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            stream(), media_type="text/event-stream",
            headers={"x-demo-model-calls": str(state["model_calls"]),
                     "x-demo-search-calls": str(state["search_calls"]),
                     "x-demo-fetch-calls": str(state["fetch_calls"])},
        )

    return app
