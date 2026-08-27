# Example scenarios

Three copy-anywhere scenario directories ship in the repo under
[`examples/scenarios/`](https://github.com/kneepoint-dev/kneepoint/tree/main/examples/scenarios).
Each contains `kneepoint.yaml`, a prompt corpus, and a README on adapting it.

| Scenario | What it stresses | The gate |
|---|---|---|
| [support-bot](https://github.com/kneepoint-dev/kneepoint/tree/main/examples/scenarios/support-bot) | multi-turn conversations, standard chaos | `min_resolution_rate: 0.90` |
| [rag-agent](https://github.com/kneepoint-dev/kneepoint/tree/main/examples/scenarios/rag-agent) | tool-layer chaos on retrieval, input-token cost | `max_cost_per_resolved: 0.05` |
| [mcp-tool-agent](https://github.com/kneepoint-dev/kneepoint/tree/main/examples/scenarios/mcp-tool-agent) | every fault a flaky tool server produces | `min_resolution_rate: 0.80` |

All three run against the bundled mock out of the box (that's how our test
suite keeps them honest); point `target.url` at your agent to make them real.
