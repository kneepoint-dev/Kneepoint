"""The licence is Apache 2.0 from 0.2.0 onward, and every place that states it agrees.

Four things claim a licence and they can drift apart independently: the `LICENSE`
text itself, `pyproject.toml` (what PyPI and installers read), the `NOTICE` file
Apache 2.0 expects to travel with the code, and the prose — README badge and
footer, the docs landing page. The CHANGELOG has one more duty: 0.1.0 and 0.0.1
went out under MIT, and that grant cannot be withdrawn, so the entry that records
the switch has to say so.

The text is compared against the canonical file's shape, not re-typed here: the
header, the section numbering, the appendix, and the one line the appendix asks
you to fill in.
"""

import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
COPYRIGHT = "Copyright 2026 Mohan Chelluru"


def _pyproject() -> dict:
    with (ROOT / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)["project"]


def test_license_file_is_the_unmodified_apache_2_text_with_the_appendix_filled():
    text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    lines = text.splitlines()
    assert lines[1].strip() == "Apache License"
    assert lines[2].strip() == "Version 2.0, January 2004"
    assert lines[3].strip() == "http://www.apache.org/licenses/"
    # the nine numbered sections, in order, then the appendix
    heads = re.findall(r"^   (\d+)\. ([A-Z][A-Za-z ]+?)\.", text, flags=re.MULTILINE)
    assert [int(n) for n, _ in heads] == list(range(1, 10)), heads
    assert heads[2][1] == "Grant of Patent License", "the patent grant is why Apache was chosen"
    assert "APPENDIX: How to apply the Apache License to your work." in text
    assert "[yyyy]" not in text and "[name of copyright owner]" not in text, (
        "the appendix placeholders were never filled in"
    )
    assert f"   {COPYRIGHT}\n" in text
    assert "MIT" not in text


def test_notice_file_names_the_work_and_the_copyright_holder():
    notice = (ROOT / "NOTICE").read_text(encoding="utf-8")
    assert notice == f"Kneepoint\n{COPYRIGHT}\n"


def test_pyproject_declares_apache_2_and_ships_both_files():
    project = _pyproject()
    assert project["license"] == "Apache-2.0"
    assert project["license-files"] == ["LICENSE", "NOTICE"]
    for name in project["license-files"]:
        assert (ROOT / name).is_file(), f"license-files names {name}, which does not exist"
    # PEP 639: a licence expression and a `License ::` classifier must not both be set
    assert not [c for c in project["classifiers"] if c.startswith("License ::")]


@pytest.mark.parametrize(
    ("path", "must_say"),
    [
        ("README.md", "Apache 2.0 licensed"),
        ("README.md", "License-Apache_2.0"),  # the badge
        ("docs/index.md", "Open source (Apache 2.0)"),
    ],
)
def test_user_facing_copy_says_apache_2(path, must_say):
    text = (ROOT / path).read_text(encoding="utf-8")
    assert must_say in text, f"{path} no longer says {must_say!r}"
    assert not re.search(r"\bMIT\b", text), f"{path} still names MIT somewhere"


def test_readme_links_the_notice_file():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "https://github.com/kneepoint-dev/kneepoint/blob/main/NOTICE" in readme


def test_changelog_records_the_switch_and_that_earlier_releases_stay_mit():
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    start = changelog.index("## [0.2.0]")
    end = changelog.index("## [0.1.0]")
    entry = changelog[start:end]
    assert re.search(r"MIT\s*→\s*Apache License 2\.0", entry), "0.2.0 does not record the switch"
    assert "0.1.0" in entry and "remain MIT-licensed" in entry
    assert "irrevocable" in entry, (
        "the entry must say the MIT grant on the earlier releases cannot be withdrawn"
    )
