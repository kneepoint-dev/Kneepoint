from pathlib import Path

import pytest

from kneepoint.config import Scenario, ScenarioError, SloConfig, evaluate_slo, load_scenario

GOOD_YAML = """
target:
  url: http://127.0.0.1:8000/v1
  model: mock
workload:
  ramp: {from: 1, to: 10, step: 2, hold_seconds: 5}
  conversation:
    turns: {min: 2, max: 4}
    corpus: ./prompts/support/*.txt
resolution:
  check: {kind: contains, value: "[RESOLVED"}
cost:
  input_per_mtok: 3.0
  output_per_mtok: 15.0
  max_spend: 0.50
chaos:
  profile: standard
slo:
  min_resolution_rate: 0.90
"""


def _load(tmp_path: Path, text: str) -> Scenario:
    p = tmp_path / "s.yaml"
    p.write_text(text, encoding="utf-8")
    return load_scenario(p)


def test_full_scenario_parses(tmp_path):
    sc = _load(tmp_path, GOOD_YAML)
    assert sc.target.url.endswith("/v1")
    assert sc.workload.ramp.from_ == 1 and sc.workload.ramp.to == 10
    assert sc.workload.conversation.turns.max == 4
    assert sc.resolution.check.value == "[RESOLVED"
    assert sc.cost.max_spend == 0.50
    assert {f.type for f in sc.chaos.resolved_faults()} == {
        "llm_rate_limit", "llm_server_error", "tool_timeout", "tool_malformed_json"
    }


def test_minimal_scenario_needs_only_target(tmp_path):
    sc = _load(tmp_path, "target: {url: http://x/v1}")
    assert sc.chaos.profile == "off"
    assert sc.chaos.resolved_faults() == []
    assert sc.cost is None


def test_typo_fails_with_field_name(tmp_path):
    with pytest.raises(ScenarioError) as err:
        _load(tmp_path, "target: {url: http://x/v1}\nworkload: {rampp: {}}")
    assert "rampp" in str(err.value)


def test_bad_yaml_syntax_is_wrapped(tmp_path):
    with pytest.raises(ScenarioError):
        _load(tmp_path, "target: [unclosed")


def test_custom_chaos_requires_faults(tmp_path):
    with pytest.raises(ScenarioError):
        _load(tmp_path, "target: {url: http://x/v1}\nchaos: {profile: custom}")


def test_evaluate_slo():
    slo = SloConfig(p95_total_ms=2000, min_resolution_rate=0.9, max_cost_per_resolved=0.5)
    assert evaluate_slo(slo, p95_ms=1500, resolution=0.95, cost_per_resolved=0.4) == []
    breaches = evaluate_slo(slo, p95_ms=2500, resolution=0.8, cost_per_resolved=None)
    assert len(breaches) == 3          # slow, under-resolved, cost set-but-unmeasured
    assert evaluate_slo(SloConfig(), p95_ms=None, resolution=None, cost_per_resolved=None) == []
