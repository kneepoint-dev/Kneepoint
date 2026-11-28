# Workload design

Cache hits can come from a static system prompt and tools, earlier steps in the
same request, earlier turns in a session, popular content, or replay of a small
test corpus. The first three are structural; set content popularity explicitly
and disclose replay. Unique content can still have substantial structural caching.

Bracket the same system with three content policies: a unique Floor
(`without_replacement`), a Realistic popularity curve (`zipf`, exponent 1.0 as an
assumption), and a small replay Ceiling (`uniform`). Report measured cached-token
fraction, throughput and p95 together for each. A replay ceiling is unsuitable as
a planning estimate. Unique sampling raises `CorpusExhausted` rather than wrapping.

Set eight dimensions explicitly: arrival, session turns and think time, weighted
task mix, input lengths, output lengths, popularity, cache state, and faults.
Every request needs ground truth or a rubric. State unsupported dimensions and
assumptions; a fixed output cap alone does not define an output distribution.
The current generator uses closed-loop concurrency, bounded turn counts and
think times. It does not implement open-loop arrival distributions.

Size a unique corpus for at least the total requests across all unique holds,
using disjoint partitions when splitting by level. For a realistic arm, aim for
at least ten times the requests of the longest hold, so the tail stays unique.
Verify these bounds against observed request counts before claiming coverage.

See `workloads/agent-knowledge.yaml` in the source distribution for a knowledge
agent example. `workload.conversation.sampling` accepts `policy` (`uniform`,
`without_replacement`, `zipf`), a positive `exponent`, and optional positive
`task_weights` summing to 1. A JSON corpus is an ordered array of item objects;
its order sets Zipf rank, including within each weighted class. Text globs keep
the original uniform default. Seeds reproduce draw order, not concurrent network
completion order. Each request record's optional `selection` retains item ID,
rank, template, length class, off-topic and first-use status; repeated attempts
have distinct `request_id`s and retain the exact sent `traceparent` (null if off).

`deadline_seconds` closes an exchange past its wall deadline, records a timeout,
and suppresses retries for that attempt. Legacy records without new fields load.
Use `--selection-log workload/selection.jsonl` to append a separate selection
stream with request IDs.

Use `kneepoint run --health-log injector/health.jsonl` for opt-in one-second timer
drift, event-loop scheduling delay, process CPU (% of one core), host CPU, TCP
connection count and in-flight requests. An unreadable counter is null with a
reason. In-flight means dispatched attempts awaiting a result; TCP connections
include idle keep-alive connections. Health sampling adds measurable overhead;
measure it on the injector host before interpreting a ramp.
