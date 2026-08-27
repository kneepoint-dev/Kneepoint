"""The GitHub issue forms must be readable by a strict YAML 1.1 parser.

GitHub parses issue forms with libyaml, which is lenient about a `?` inside a
flow mapping (`{label: Does it break?}`). PyYAML's pure-Python ``SafeLoader``
is not, and raises ``ParserError`` on the whole file — so a template can render
perfectly on GitHub while being unreadable to the most-installed Python YAML
parser. These tests load every template with the *pure* loader, on purpose:
``yaml.safe_load`` may silently pick the C loader when it is available and
would then pass on exactly the file this exists to catch.
"""

from pathlib import Path

import pytest
import yaml

TEMPLATE_DIR = Path(".github/ISSUE_TEMPLATE")
FORMS = sorted(p for p in TEMPLATE_DIR.glob("*.yml") if p.name != "config.yml")

# https://docs.github.com/en/communities/using-templates-to-encourage-useful-issues-and-pull-requests/syntax-for-githubs-form-schema
FORM_ELEMENT_TYPES = {"markdown", "textarea", "input", "dropdown", "checkboxes"}


def _load_pure(path: Path):
    """Parse with the pure-Python loader regardless of whether libyaml is installed."""
    with path.open(encoding="utf-8") as fh:
        return yaml.load(fh, Loader=yaml.SafeLoader)  # noqa: S506 - SafeLoader by name


def test_templates_exist():
    assert FORMS, "no issue forms found — is the test running from the repo root?"
    assert (TEMPLATE_DIR / "config.yml").exists()


@pytest.mark.parametrize("path", FORMS, ids=lambda p: p.stem)
def test_issue_form_is_strict_yaml_and_well_formed(path):
    form = _load_pure(path)
    assert isinstance(form, dict), path
    for key in ("name", "description", "body"):
        assert form.get(key), f"{path}: missing {key!r}"
    for element in form["body"]:
        assert element["type"] in FORM_ELEMENT_TYPES, f"{path}: {element['type']!r}"
        if element["type"] != "markdown":
            assert element["attributes"].get("label"), f"{path}: element without a label"


def test_config_is_strict_yaml():
    cfg = _load_pure(TEMPLATE_DIR / "config.yml")
    assert isinstance(cfg.get("blank_issues_enabled"), bool)
    for link in cfg.get("contact_links", []):
        assert link["url"].startswith("https://")


def test_question_mark_in_flow_mapping_is_the_defect_this_guards():
    """Negative control: the shape that used to be in new_fault_type.yml:17 does
    fail the pure loader, so the parametrized test above is a real check."""
    with pytest.raises(yaml.YAMLError):
        yaml.load("attributes: {label: Does it break?}", Loader=yaml.SafeLoader)
    assert yaml.load('attributes: {label: "Does it break?"}', Loader=yaml.SafeLoader) == {
        "attributes": {"label": "Does it break?"}
    }
