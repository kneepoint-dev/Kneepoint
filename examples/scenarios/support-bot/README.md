# Scenario: support bot

The canonical kneepoint scenario: simulated customers hold 1–3 turn
conversations sampled from a real prompt distribution while concurrency ramps
1→50, with the standard chaos profile injecting rate limits, server errors,
and tool faults.

**Adapt it to your agent:**
1. Point `target.url` at your agent's OpenAI-compatible endpoint (set
   `auth_env: MY_KEY_VAR` if it needs a bearer token).
2. Replace `prompts/*.txt` with ~10–50 real user questions, one per file.
3. Replace the resolution check with whatever marks a *solved* ticket in your
   agent's final answer — a literal string (`kind: contains`) or a regex
   (`kind: regex`). No deterministic marker? Configure `resolution.judge`
   instead (see the docs).
4. Set real prices from your model's price sheet, and a `max_spend` you can
   afford to lose.

`slo.min_resolution_rate: 0.90` makes this a CI gate: the run exits 1 when
quality under load drops below 90%.
