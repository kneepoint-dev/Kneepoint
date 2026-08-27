# Kneepoint

**Find where your AI agent breaks** — before your users do.

Your agent passes its evals at concurrency 1. Production is not concurrency 1.
Kneepoint ramps concurrent multi-turn sessions against your agent, judges
whether tasks actually got *resolved*, prices every token, and injects the
faults production will inject for you — then answers three questions in one
self-contained HTML report:

1. **The knee point** — where latency/quality stops being flat and cliffs.
2. **$ / resolved task** — spend ÷ tasks actually solved.
3. **Resilience score** — how much resolution survives injected chaos.

[Quickstart →](quickstart.md){ .md-button .md-button--primary }
[Scenario reference →](scenarios.md){ .md-button }

Open source (Apache 2.0), runs entirely on your machine, and the demo needs zero API keys.
