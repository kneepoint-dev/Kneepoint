"""Report guidance and stable documentation URL contracts."""

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
    below_verdict = html.split('<div class="verdict">', 1)[1]
    assert len(lines) == below_verdict.count("<h2>") + 1 == 13
    assert all(line.startswith("How to read this.") for line in lines)
    assert all(line.endswith("→") for line in lines)
    # one distinctive claim per section, in document order
    expected = [
        "latency-tolerance threshold",                # verdict
        "where p95 bends",                            # knee curve
        "Read verdict coverage before the rate",       # quality populations
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
    assert "latency-tolerance threshold" in lines[0]
    assert "where p95 bends" in lines[1]


def test_how_to_read_links_use_supported_slugs():
    src = TEMPLATE.read_text(encoding="utf-8")
    calls = re.findall(r'howto\("([a-z-]+)", "([^"]+)"\)', src)
    assert len(calls) == 13
    assert {slug for slug, _ in calls} == {
        "throughput", "foundations", "quality", "latency", "stability", "cost",
    }
    assert all(title for _, title in calls)
    assert 'href="https://docs.kneepoint.dev/book/{{ slug }}/"' in src


def test_report_retains_its_interpretation_guidance(tmp_path):
    lines = " ".join(_howto_lines(_full_report(tmp_path))).lower()
    heads = {
        "latency-tolerance threshold", "not the onset of queueing",
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
