"""The version has one source, and the two things that quote it must agree with it.

`pyproject.toml` is the source. Two places quote it:

* `CHANGELOG.md` — a version that ships without an entry describing it is a
  version nobody can evaluate before installing;
* the installed distribution — every JSONL line and sidecar is stamped with
  `importlib.metadata.version("kneepoint")`, which is whatever the package was
  *installed* as. An editable install that predates a bump keeps stamping the
  old number into new files, so a stale install is a real defect, not noise.
"""

import re
import tomllib
from importlib.metadata import version as pkg_version
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _pyproject_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)["project"]["version"]


def test_version_is_semver():
    assert re.fullmatch(r"\d+\.\d+\.\d+", _pyproject_version())


def test_changelog_has_an_entry_for_this_version():
    version = _pyproject_version()
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    headings = re.findall(r"^## \[([^\]]+)\]", changelog, flags=re.MULTILINE)
    assert version in headings, (
        f"pyproject says {version} but CHANGELOG.md has no '## [{version}]' heading; "
        f"headings present: {headings}"
    )


def test_changelog_newest_entry_is_this_version():
    # The top entry is the one being shipped. An `[Unreleased]` section above it
    # is fine only when it exists — Keep a Changelog's convention — so skip it.
    version = _pyproject_version()
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    headings = re.findall(r"^## \[([^\]]+)\]", changelog, flags=re.MULTILINE)
    newest = next(h for h in headings if h.lower() != "unreleased")
    assert newest == version


def test_installed_distribution_matches_pyproject():
    installed = pkg_version("kneepoint")
    assert installed == _pyproject_version(), (
        f"installed kneepoint is {installed}, pyproject.toml says {_pyproject_version()}: "
        "the install is stale and would stamp the old version into every file it writes "
        "— reinstall (`pip install -e .`)"
    )
