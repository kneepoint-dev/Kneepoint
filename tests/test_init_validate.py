from typer.testing import CliRunner

from kneepoint.cli import app


def test_init_writes_scenario_and_corpus(tmp_path):
    result = CliRunner().invoke(app, ["init", "--out", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "kneepoint.yaml").exists()
    prompts = list((tmp_path / "prompts" / "support").glob("*.txt"))
    assert len(prompts) >= 5
    # the generated file must validate with our own validator
    result2 = CliRunner().invoke(app, ["validate", str(tmp_path / "kneepoint.yaml")])
    assert result2.exit_code == 0, result2.output
    assert "valid" in result2.output.lower()


def test_starter_budget_covers_its_own_ramp(tmp_path):
    """The starter must not trip its own budget rail: its 1..50 mock ramp
    estimates ~$4 of play money, so the cap must clear that — a stranger's
    first `kneepoint run` exiting 3 is a broken quickstart."""
    from kneepoint.config import load_scenario

    CliRunner().invoke(app, ["init", "--out", str(tmp_path)])
    sc = load_scenario(tmp_path / "kneepoint.yaml")
    assert sc.cost.max_spend >= 5.0


def test_init_refuses_to_overwrite(tmp_path):
    (tmp_path / "kneepoint.yaml").write_text("x", encoding="utf-8")
    result = CliRunner().invoke(app, ["init", "--out", str(tmp_path)])
    assert result.exit_code != 0
    assert (tmp_path / "kneepoint.yaml").read_text(encoding="utf-8") == "x"


def test_validate_rejects_bad_file(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("target: {url: http://x/v1}\nchaos: {profile: nope}", encoding="utf-8")
    result = CliRunner().invoke(app, ["validate", str(bad)])
    assert result.exit_code == 1
    assert "chaos" in result.output
