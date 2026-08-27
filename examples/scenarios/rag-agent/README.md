# Scenario: RAG agent

RAG agents fail differently under load: the LLM keeps streaming confident
prose while the retrieval tool behind it times out or returns garbage — the
classic silent failure. This scenario aims chaos at the tool layer
(`tool_timeout`, `tool_malformed_json`) and gates on the **quality curve**
and **$/resolved task** rather than latency alone.

**Adapt it to your agent:**
1. Point `target.url` at your RAG agent's endpoint.
2. Replace `prompts/*.txt` with questions that require retrieval from *your*
   corpus (doc lookups, comparisons, summaries — not general knowledge).
3. Change the check to your grounding marker, e.g. a citation regex:
   `check: {kind: regex, value: '\[(source|doc|\d+)\]'}` — an answer that
   cites nothing didn't ground, and shouldn't count as resolved.
4. To fault your agent's real retrieval endpoint, route it through the chaos
   tool proxy (see `examples/chaos_demo.py` and the chaos docs) and have your
   agent forward the `x-kneepoint-session` header for per-session attribution.

`max_cost_per_resolved: 0.05` is the interesting gate here: growing contexts
make RAG input tokens balloon under retries, and this catches it.
