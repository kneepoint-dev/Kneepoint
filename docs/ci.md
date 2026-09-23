# CI gate

Kneepoint is a test: it exits non-zero when your agent regresses, so it slots
into CI like any other check.

## Exit codes

| Code | Meaning | Comes from |
|---|---|---|
| 0 | run completed, every configured SLO met (or none configured) | — |
| 1 | **Failed or unknown SLO** — `SLO fail - <metric>` identifies a measured breach; `SLO unknown - <metric>` identifies insufficient evidence. Both fail the CI gate. Every configured check prints its state, threshold, measured value (nullable), and reason. Also: invalid scenario file, or an empty configured `auth_env`. | `slo:` block |
| 2 | usage error (bad flag, malformed `--ramp`, …) | CLI parsing |
| 3 | **spend control stop** — observed spend reached `max_spend`, or a capped run encountered unknown usage. Active calls drain and can overshoot; partial results are reported. `--force` never bypasses the stop | `cost.max_spend` |

Budget control stops retain exit **3**, even when some checks fail or are unknown.
A check can pass on observed records without proving the configured ramp completed.
No configured SLO is shown as "No SLO configured", never an invented pass.
`kneepoint report` recomputes these results with any price overrides, but its exit
code reports rendering success/failure; it does not apply the `run` CI gate.

## GitHub Actions

```yaml
name: agent-load-test
on:
  pull_request:
  schedule:
    - cron: "0 3 * * *"
jobs:
  kneepoint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: {python-version: "3.12"}
      - run: pip install kneepoint
      # start your agent here (service container, docker compose, staging URL...)
      - run: kneepoint run --scenario kneepoint.yaml --out reports
        env:
          AGENT_API_KEY: ${{ secrets.AGENT_API_KEY }}   # if target.auth_env is set
      - uses: actions/upload-artifact@v4
        if: always()                    # keep the report especially on failure
        with: {name: kneepoint-report, path: reports/}
```

Practical notes:

- **Pin your seed** (`seed: 42` in the scenario) so a red build reruns the
  same sampled prompts and chaos decisions.
- **Budget-cap CI runs** even against staging — a misconfigured ramp on a
  paid model is a real bill. Exit 3 fails the build *before* spending.
- Gate PRs on a small fast scenario (`to: 10`, short holds) and run the full
  ramp nightly — knee detection wants ≥10 samples per level to trust a level.
- The uploaded HTML report is self-contained: download the artifact, open it,
  done.

## Our own nightly

This repo dogfoods the gate: a scheduled workflow runs `kneepoint demo`
against the bundled agent every night and uploads the report —
[see it run](https://github.com/kneepoint-dev/kneepoint/actions/workflows/nightly-demo.yml).
