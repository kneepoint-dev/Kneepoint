from pathlib import Path

import pytest
from typer.testing import CliRunner

from kneepoint.cli import app
from kneepoint.config import load_scenario

SCENARIOS = [
    Path("examples/scenarios/support-bot/kneepoint.yaml"),
    Path("examples/scenarios/rag-agent/kneepoint.yaml"),
    Path("examples/scenarios/mcp-tool-agent/kneepoint.yaml"),
]


def test_demo_yaml_still_loads_with_scenario_relative_corpus():
    # examples/kneepoint.yaml switches to './prompts/support/*.txt' in this task;
    # it must still validate and its glob must resolve from the file's own dir
    sc = load_scenario(Path("examples/kneepoint.yaml"))
    assert sc.workload.conversation.corpus == "./prompts/support/*.txt"


@pytest.mark.parametrize("path", SCENARIOS, ids=lambda p: p.parent.name)
def test_example_scenarios_validate(path):
    sc = load_scenario(path)
    assert sc.resolution.check is not None          # every example judges
    assert sc.workload.conversation.corpus          # every example has a corpus
    assert (path.parent / "README.md").exists()
    if "think_time_ms" in path.read_text(encoding="utf-8"):
        # ThinkTime ignores unknown keys, so a wrong spelling (min/max instead
        # of min_ms/max_ms) validates but silently zeroes the think time
        assert sc.workload.conversation.think_time_ms.max_ms > 0


def test_scenario_corpus_resolves_relative_to_scenario_file(mock_agent_url, tmp_path):
    """A scenario dir must be copy-anywhere: its corpus glob anchors to the
    YAML's own directory, not to whatever CWD kneepoint runs from."""
    scenario_dir = tmp_path / "anywhere"
    (scenario_dir / "prompts").mkdir(parents=True)
    (scenario_dir / "prompts" / "q1.txt").write_text("only prompt", encoding="utf-8")
    (scenario_dir / "s.yaml").write_text(
        f"""
target: {{url: {mock_agent_url}}}
workload:
  ramp: {{from: 1, to: 1, step: 1, hold_seconds: 0.5}}
  conversation:
    corpus: ./prompts/*.txt
resolution:
  check: {{kind: contains, value: "[RESOLVED"}}
""",
        encoding="utf-8",
    )
    result = CliRunner().invoke(
        app, ["run", "--scenario", str(scenario_dir / "s.yaml"), "--out", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    assert "Resolution rate:" in result.output


def test_support_bot_scenario_runs_against_mock(mock_agent_url, tmp_path):
    """Flags override the scenario: point the committed example at the test mock
    and shrink the ramp so this stays fast."""
    result = CliRunner().invoke(
        app,
        [
            "run", "--scenario", str(SCENARIOS[0]),
            "--target", mock_agent_url,
            "--ramp", "1..2", "--step", "1", "--hold-seconds", "0.5",
            "--chaos", "off",
            "--out", str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output
    assert list(tmp_path.glob("run-*-report.html"))
