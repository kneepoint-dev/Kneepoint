# Testing against a local model

Kneepoint measures **agents**, not model servers. Ramping concurrency at a bare
Ollama or MLX endpoint measures inference queueing — that is a serving
benchmark, and tools like GuideLLM already do it well. Point Kneepoint at a
bare endpoint and the resolution and resilience pillars have nothing to work
with: there is no tool to break and no task to resolve.

[`examples/local_agent/`](https://github.com/kneepoint-dev/kneepoint/tree/main/examples/local_agent)
closes that gap. It is a small ASGI app that puts an agent around a local
model: a system prompt, a naive tool step that chaos can break, and a verbatim
relay of the model's stream.

## Run it

```zsh
# 1. serve a model
brew install ollama && brew services start ollama
ollama pull gemma4:12b

# 2. serve the agent wrapper in front of it
LOCAL_AGENT_MODEL=gemma4:12b \
  python -m uvicorn examples.local_agent.app:app --port 8000

# 3. point kneepoint at the wrapper, not at Ollama
kneepoint run --target http://127.0.0.1:8000/v1 --model gemma4:12b \
  --ramp 1..16 --step 3 --hold-seconds 60 --chaos standard
```

## Configuration

| Variable | Default | What it does |
|---|---|---|
| `LOCAL_AGENT_UPSTREAM` | `http://127.0.0.1:11434/v1` | the model server's OpenAI-compatible base URL |
| `LOCAL_AGENT_MODEL` | *(from the request)* | override the model name Kneepoint sends |
| `LOCAL_AGENT_TOOL_URL` | *(in-process)* | tool endpoint — point this at the chaos proxy so tool faults reach the agent |
| `LOCAL_AGENT_TOOL_TIMEOUT_MS` | `1000` | how long the naive tool step waits before giving up |
| `LOCAL_AGENT_REASONING_EFFORT` | *(unset)* | forwarded upstream as `reasoning_effort` |

## Two things that will bite you

**Thinking models turn TTFT into a reasoning-length measurement.** On a model
that reasons before answering, time-to-first-token is time-to-end-of-thinking.
Measured on gemma4:12b over 20 single-turn sessions at concurrency 1: median
TTFT **92.7 s** with thinking on, **635 ms** with
`LOCAL_AGENT_REASONING_EFFORT=none`. Both are legitimate numbers; they are
answers to different questions. Set the knob deliberately and say which way you
set it whenever you publish a number.

**Baseline format-following before you read a resolution rate.** Run ~20
sessions at concurrency 1 with chaos off and count how many answers carry your
resolution marker. That number is the ceiling for every resolution rate the
model can produce under load. Without it you cannot tell "the agent broke under
load" from "the model never followed the format" — a 65% rate under chaos means
nothing if the model only manages 70% when nothing is wrong.

The tool result is part of this. `local_agent`'s built-in knowledge base
returns real article text rather than an opaque id, because a model handed an
opaque id will correctly *refuse* to claim the task is resolved — and then the
resolution rate measures your fixture instead of your agent.

## Collocation

Running the generator, the wrapper and the model server on one machine means
they contend for the same cores and memory bandwidth. That is fine for finding
your own knee; it is not a clean comparison between models or runtimes. Say so
in anything you publish.
