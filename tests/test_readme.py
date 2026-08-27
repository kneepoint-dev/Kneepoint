"""The README is the first thing a stranger reads, and PyPI renders it as the long
description — so its scenario must validate, its commands must exist, its exit
codes must match the CI doc, and every link into this repository must resolve.
"""

import re
from pathlib import Path

import pytest
import yaml

from kneepoint.cli import app
from kneepoint.config import Scenario

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
REPO = "https://github.com/kneepoint-dev/kneepoint"

_FENCE = re.compile(r"```(\w+)\n(.*?)```", re.DOTALL)
_LINK = re.compile(r"\]\(([^)\s]+)\)")


def _blocks(lang: str) -> list[str]:
    return [body for tag, body in _FENCE.findall(README) if tag == lang]


def test_scenario_block_is_a_valid_scenario():
    """The YAML the README shows must load under the same strict schema `run` uses."""
    blocks = [b for b in _blocks("yaml") if b.lstrip().startswith("target:")]
    assert len(blocks) == 1, "expected exactly one scenario example in the README"
    scenario = Scenario.model_validate(yaml.safe_load(blocks[0]))
    assert scenario.cost is not None and scenario.cost.max_spend is not None
    assert scenario.slo.min_resolution_rate is not None
    assert scenario.chaos.profile == "standard"


def test_every_command_named_exists():
    """Lower-case `kneepoint <word>` is always a command invocation in the README
    (prose capitalises the name), so every such word must be a registered command."""
    registered = {c.name or c.callback.__name__ for c in app.registered_commands}
    named = set(re.findall(r"\bkneepoint ([a-z]+)\b", README))
    assert named, "the README names no commands?"
    assert named <= registered, f"README names commands that do not exist: {named - registered}"
    for must in ("demo", "init", "validate", "run", "report", "proxy"):
        assert must in named, f"the README never shows `kneepoint {must}`"


def test_exit_codes_match_ci_doc():
    ci = (ROOT / "docs/ci.md").read_text(encoding="utf-8")
    readme_codes = set(re.findall(r"`([0-3])` ", README))
    assert readme_codes == {"0", "1", "2", "3"}
    for code in readme_codes:
        assert re.search(rf"^\| {code} \|", ci, re.MULTILINE), f"exit code {code} not in the CI doc"
    assert "budget" in README and "SLO breach" in README


def test_links_into_the_repo_resolve():
    """Absolute so PyPI renders them — but they still have to point at real files."""
    targets = _LINK.findall(README)
    repo_links = [
        t for t in targets if t.startswith(REPO + "/blob/") or t.startswith(REPO + "/tree/")
    ]
    assert repo_links, "no repository links found"
    missing = []
    for t in repo_links:
        path = re.sub(rf"^{re.escape(REPO)}/(?:blob|tree)/main/", "", t).split("#", 1)[0]
        if not (ROOT / path).exists():
            missing.append(t)
    assert not missing, f"README links to paths that do not exist: {missing}"
    relative = [t for t in targets if not re.match(r"^[a-z]+:", t) and not t.startswith("#")]
    assert not relative, f"relative links do not render on PyPI: {relative}"


@pytest.mark.parametrize("path", ["docs/assets/demo.gif"])
def test_images_exist(path):
    imgs = re.findall(r"!\[[^\]]*\]\(([^)\s]+)\)", README)
    assert any(i.endswith(path) for i in imgs), f"README no longer embeds {path}"
    assert (ROOT / path).exists()
