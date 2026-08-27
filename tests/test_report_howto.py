"""The report's "How to read this" lines — one per section, worded from the Book.

The report is a teaching surface for someone who has never used Kneepoint. The
standing instruction is to *source* each line from `docs/book/`, not to write
new teaching copy, so these tests check the report side against the Book side:
every line links to a chapter that ships, shows that chapter's own title, and
carries a load-bearing phrase that is in the chapter verbatim.
"""

import re
from pathlib import Path

from kneepoint.analyze.errors import errors_by_level
from kneepoint.analyze.knee import KneeResult
from kneepoint.analyze.resolution import LevelResolution
from kneepoint.analyze.retry import retry_by_level
from kneepoint.collect.schemas import RequestRecord
from kneepoint.report.html import write_report
from tests.test_report_html import _cost, _meta, _res, _run_meta, _stats, _streamed_stats

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "kneepoint/report/templates/report.html.j2"
BOOK = ROOT / "docs/book"


def _howto_lines(html: str) -> list[str]:
    """The text of every how-to-read line, tags stripped, whitespace folded."""
    return [
        " ".join(re.sub(r"<[^>]+>", "", m.group(1)).split())
        for m in re.finditer(r'<p class="howto">(.*?)</p>', html, re.S)
    ]


def _full_report(tmp_path) -> str:
    recs = [RequestRecord(session_id="s", concurrency=1, started_at=0.0, total_ms=1.0,
                          ok=True)] * 10
    return write_report(
        tmp_path / "full.html", meta=_meta(), stats=_streamed_stats(),
        knee=KneeResult(concurrency=5, method="p95_doubling"),
        quality=[LevelResolution(concurrency=c, judged=20, resolved_rate=r)
                 for c, r in [(1, 1.0), (5, 0.9), (10, 0.8)]],
        cost=_cost(), res=_res(), retry_levels=retry_by_level(recs, []),
        error_levels=errors_by_level(recs), run_meta=_run_meta(),
    ).read_text(encoding="utf-8")


def test_every_section_carries_a_how_to_read_line(tmp_path):
    """The verdict and every <h2> section below it end with a line that says
    what the figure means and which way to read it."""
    html = _full_report(tmp_path)
    lines = _howto_lines(html)
    assert len(lines) == html.count("<h2>") + 1 == 12
    assert all(line.startswith("How to read this.") for line in lines)
    assert all(line.endswith("→") for line in lines)
    # one distinctive claim per section, in document order
    expected = [
        "plan for the low end",                       # verdict
        "where p95 bends",                            # knee curve
        "caused by load",                             # quality under load
        "share of judged sessions",                   # quality curve
        "TTFT rising, TPOT flat",                     # TTFT vs TPOT
        "hides stutter",                              # ITL
        "Which column answers which question",        # latency breakdown
        "capacity signal",                            # errors
        "turns a slowdown into an outage",            # retry
        "prices the outcome, not the activity",       # cost
        "100 means the faults made no difference",    # resilience
        "one run is an anecdote",                     # run metadata
    ]
    for line, phrase in zip(lines, expected, strict=True):
        assert phrase in line, (phrase, line)


def test_how_to_read_lines_are_not_shown_for_sections_that_rendered_nothing(tmp_path):
    """A placeholder already says why the section is empty; guidance on reading
    a chart that is not there would be noise."""
    html = write_report(
        tmp_path / "bare.html", meta=_meta(), stats=_stats(), knee=None,
        quality=[], cost=None, res=None,
    ).read_text(encoding="utf-8")
    lines = _howto_lines(html)
    assert len(lines) == 2                      # the verdict and the knee curve
    assert "plan for the low end" in lines[0] and "where p95 bends" in lines[1]


def test_how_to_read_lines_link_to_book_chapters_that_exist():
    """Every link in the template points at a chapter that ships in docs/book/,
    so a renamed chapter fails here rather than as a 404 in someone's report."""
    src = TEMPLATE.read_text(encoding="utf-8")
    calls = re.findall(r'howto\("([a-z-]+)", "([^"]+)"\)', src)
    slugs = {slug for slug, _ in calls}
    assert slugs == {"throughput", "foundations", "quality", "latency", "stability", "cost"}
    for slug, title in calls:
        chapter = BOOK / f"{slug}.md"
        assert chapter.is_file(), slug
        # the chapter title shown to the reader is the chapter's own H1, written
        # plain — autoescape turns its "&" into "&amp;" on the way out
        h1 = chapter.read_text(encoding="utf-8").splitlines()[0]
        assert h1 == "# " + title, (slug, h1, title)
    # and the rendered href is the mkdocs URL for that chapter
    assert 'href="https://docs.kneepoint.dev/book/{{ slug }}/"' in src


def test_how_to_read_wording_is_the_books_wording(tmp_path):
    """Each line's load-bearing phrase is in the chapter it draws on, verbatim —
    this guards the report from drifting into teaching copy the Book does not
    back. A chapter that changes its mind must take the report with it."""
    lines = " ".join(_howto_lines(_full_report(tmp_path))).lower()
    chapters = {p.stem: " ".join(p.read_text(encoding="utf-8").split()).lower()
                for p in BOOK.glob("*.md")}
    sourced = [
        ("throughput", "plan for the low end"),
        ("throughput", "not a limit you must never exceed"),
        ("throughput", "kneedle corroborates"),
        ("foundations", "an absence of data"),
        ("foundations", "the median stays flat long after the system starts struggling"),
        ("reading-charts", "a suppressed point, not a rendering bug"),
        ("quality", "caused by load"),
        ("quality", "never silently counted as failures"),
        ("quality", "a single-run drift chart is not evidence"),
        ("recipes", "decode saturation"),
        ("latency", "hides stutter"),
        ("latency", "fixed flush interval hiding a slowing decoder"),
        ("latency", "telling them apart is most of diagnosis"),
        ("stability", "errors present from level 1 are a configuration bug"),
        ("stability", "turns a slowdown into an outage"),
        ("stability", "faults made no difference"),
        ("stability", "nobody wrote that error path"),
        ("cost", "prices the outcome, not the activity"),
        ("cost", "cost knee that arrives before the latency knee"),
        ("cost", "projection at named rates"),
        ("foundations", "one run is an anecdote"),
        ("foundations", "not the best result"),
    ]
    for chapter, phrase in sourced:
        assert phrase in chapters[chapter], ("not in the Book:", chapter, phrase)
    # and the report carries each idea — a near-literal is allowed where the
    # sentence had to be recast for the report (e.g. "level 1" → "first level"),
    # so the check is on the phrase's distinctive head
    heads = {
        "plan for the low end", "not a limit you must never exceed",
        "kneedle corroborates", "an absence of data",
        "median stays flat long after the system starts struggling",
        "a suppressed point, not a rendering bug", "caused by load",
        "never counted as failures", "single-run drift curve is not evidence",
        "decode saturation", "hides stutter",
        "fixed flush interval hiding a slowing decoder",
        "telling them apart is most of diagnosis", "configuration bug",
        "turns a slowdown into an outage", "faults made no difference",
        "nobody wrote that error path", "prices the outcome, not the activity",
        "cost knee that arrives before the latency knee",
        "projection at the configured rates", "one run is an anecdote",
        "never the best",
    }
    for head in heads:
        assert head in lines, ("not in the report:", head)
