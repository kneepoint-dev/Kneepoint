# Roadmap

- **The Kneepoint Index** — published knee / $/resolved / resilience
  benchmarks of popular agent stacks, rerun as they release.
- **Native MCP target adapter** — speak MCP directly and fault-inject tool
  calls first-class (today: HTTP proxy pattern, see Chaos testing).
- **More injectors** — `stream_cut`, `tool_error`, `tool_stale_data`,
  `slow_tokens`. Each is one file; each is a good first issue.
- **N-run variance mode** — error bars on every number.

Shipped: `kneepoint report <run.jsonl>` — re-render the HTML from a run's
stored files, or recover a report that was lost (see
[Output format](output-format.md#re-rendering-the-report)).

Shipped: cross-process tool-fault attribution — `kneepoint proxy --fault-log`
plus `kneepoint run --fault-log` gives the full resilience grid with an
externally-launched proxy (see [Chaos testing](chaos.md)).

Opinions welcome — [open a discussion](https://github.com/kneepoint-dev/kneepoint/discussions).
