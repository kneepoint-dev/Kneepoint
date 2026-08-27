"""Scenario config: the declarative YAML of docs/scenarios.md, Pydantic-validated."""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from kneepoint.analyze.cost import CostRates
from kneepoint.chaos.faults import STANDARD_PROFILE, FaultSpec
from kneepoint.generator.session import RetryPolicy, ThinkTime, TurnsSpec
from kneepoint.judge.deterministic import CheckConfig
from kneepoint.judge.llm_judge import JudgeConfig


class ScenarioError(Exception):
    """Bad scenario file: YAML syntax or schema violation, with a friendly message."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TargetConfig(_Strict):
    type: Literal["openai-compatible"] = "openai-compatible"
    url: str
    model: str = "mock"
    auth_env: str | None = None


class RampConfig(_Strict):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    from_: int = Field(1, alias="from", ge=1)
    to: int = 50
    step: int = 5
    hold_seconds: float = 15.0

    @model_validator(mode="after")
    def _ordered(self) -> "RampConfig":
        if self.to < self.from_:
            raise ValueError(f"ramp needs from <= to, got {self.from_}..{self.to}")
        return self


class ConversationConfig(_Strict):
    turns: TurnsSpec = TurnsSpec()
    corpus: str | None = None
    think_time_ms: ThinkTime = ThinkTime()
    retry: RetryPolicy = RetryPolicy()


class WorkloadConfig(_Strict):
    sessions: int | None = None  # validated, unused in v0 (plan Open Question #6)
    ramp: RampConfig = RampConfig()
    conversation: ConversationConfig = ConversationConfig()


class ResolutionConfig(_Strict):
    check: CheckConfig | None = None
    judge: JudgeConfig | None = None


class CostConfig(CostRates):
    model_config = ConfigDict(extra="forbid")
    max_spend: float | None = None


class ChaosConfig(_Strict):
    profile: Literal["off", "standard", "custom"] = "off"
    faults: list[FaultSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def _custom_needs_faults(self) -> "ChaosConfig":
        if self.profile == "custom" and not self.faults:
            raise ValueError("chaos profile 'custom' requires a non-empty faults list")
        return self

    def resolved_faults(self) -> list[FaultSpec]:
        if self.profile == "standard":
            return STANDARD_PROFILE
        return self.faults if self.profile == "custom" else []


class SloConfig(_Strict):
    p95_total_ms: float | None = None
    min_resolution_rate: float | None = None
    max_cost_per_resolved: float | None = None


class Scenario(_Strict):
    target: TargetConfig
    workload: WorkloadConfig = WorkloadConfig()
    resolution: ResolutionConfig = ResolutionConfig()
    cost: CostConfig | None = None
    chaos: ChaosConfig = ChaosConfig()
    slo: SloConfig = SloConfig()
    seed: int = 0


def load_scenario(path: Path) -> Scenario:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ScenarioError(f"{path}: invalid YAML - {exc}") from exc
    if not isinstance(raw, dict):
        raise ScenarioError(f"{path}: scenario must be a YAML mapping")
    try:
        return Scenario.model_validate(raw)
    except ValidationError as exc:
        lines = [
            f"  {'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
        ]
        raise ScenarioError(f"{path}: invalid scenario\n" + "\n".join(lines)) from exc


def evaluate_slo(
    slo: SloConfig,
    *,
    p95_ms: float | None,
    resolution: float | None,
    cost_per_resolved: float | None,
) -> list[str]:
    """Human-readable breach list; an SLO that was set but not measured is a breach."""
    breaches: list[str] = []

    def check(threshold, measured, name, ok) -> None:
        if threshold is None:
            return
        if measured is None:
            breaches.append(f"{name}: SLO set but not measured in this run")
        elif not ok(measured, threshold):
            breaches.append(f"{name}: measured {measured:.4g} vs limit {threshold:.4g}")

    check(slo.p95_total_ms, p95_ms, "p95_total_ms", lambda m, t: m <= t)
    check(slo.min_resolution_rate, resolution, "min_resolution_rate", lambda m, t: m >= t)
    check(slo.max_cost_per_resolved, cost_per_resolved, "max_cost_per_resolved",
          lambda m, t: m <= t)
    return breaches


STARTER_SCENARIO = """\
# kneepoint scenario - see https://kneepoint.dev
# Run with: kneepoint run --scenario kneepoint.yaml

target:
  type: openai-compatible
  url: http://127.0.0.1:8000/v1   # the bundled mock agent (uvicorn examples.mock_agent.app:app)
  model: mock
  # auth_env: AGENT_API_KEY       # env var holding a bearer token, if your agent needs one

workload:
  ramp: {from: 1, to: 50, step: 5, hold_seconds: 20}
  conversation:
    turns: {min: 1, max: 3}
    corpus: ./prompts/support/*.txt
    retry: {max_attempts: 3, backoff_s: 0.5}   # what a real client does - and load
                                               # the generator adds when the target is
                                               # already struggling. The report shows
                                               # the amplification; set 1 to measure
                                               # capacity without it

resolution:
  check: {kind: contains, value: "[RESOLVED"}   # deterministic; use judge if no marker
  # judge: {base_url: https://api.groq.com/openai/v1, model: llama-3.1-8b-instant, sample_rate: 0.2}

cost:
  input_per_mtok: 3.00
  output_per_mtok: 15.00
  max_spend: 5.00                 # hard cap: the run stops if real spend crosses this
                                  # (the full 1..50 mock ramp estimates ~$4 of play money;
                                  #  set a cap you can afford before pointing at a paid model)

chaos:
  profile: "off"                  # "off" | standard | custom (bare off is YAML for false)

slo:                              # non-zero exit code when breached -> CI gate
  min_resolution_rate: 0.90
"""
