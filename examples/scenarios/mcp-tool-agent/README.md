# Scenario: agent with MCP tools

Tool-calling agents are only as reliable as their flakiest tool — and MCP
servers are network services that time out, rate-limit, and return garbage
like anything else. This scenario measures whether *the agent* survives that.

**How the chaos reaches MCP tools today:** kneepoint doesn't speak MCP yet — a
native adapter is on the [roadmap](../../../docs/roadmap.md). What works now,
for MCP servers on an HTTP transport: put the kneepoint chaos proxy between
your agent and the tool endpoint, exactly like `examples/chaos_demo.py` does
for the bundled agent — point the agent's tool/MCP server URL at the proxy,
and the proxy forwards cleanly except when a fault fires.

**Per-session fault attribution** (which sessions were hit by which fault)
requires your agent to echo the `x-kneepoint-session` request header on its
outbound tool calls — one line in most agent frameworks. Without it, faults
are still injected and counted globally, but the resilience grid can't break
them out per fault type from a separately-launched proxy.

**Adapt it to your agent:**
1. Point `target.url` at your agent's OpenAI-compatible endpoint.
2. Replace the prompts with tasks that actually exercise your tools.
3. Pick a resolution check that only passes when the tool result made it into
   the final answer — resolution is the whole point of tool calls.
