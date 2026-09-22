from kneepoint.analyze.knee import KneeResult, LevelStats, aggregate
from kneepoint.analyze.resolution import LevelResolution
from kneepoint.collect.schemas import RequestRecord
from kneepoint.report.chart import drift_figure, knee_figure, write_knee_chart


def _stats() -> list[LevelStats]:
    return [
        LevelStats(concurrency=c, n=50, error_rate=0.0,
                   p50_ms=v * 0.6, p95_ms=v, p99_ms=v * 1.2)
        for c, v in [(1, 1000.0), (5, 1100.0), (10, 2500.0)]
    ]


def test_write_knee_chart_produces_selfcontained_html(tmp_path):
    knee = KneeResult(concurrency=5, method="kneedle")
    out = write_knee_chart(_stats(), knee, tmp_path / "knee.html")
    assert out.exists()
    html = out.read_text(encoding="utf-8")
    assert "plotly" in html.lower()
    assert "knee @ 5" in html


def test_write_knee_chart_without_knee(tmp_path):
    out = write_knee_chart(_stats(), None, tmp_path / "knee.html")
    assert out.exists()
    assert "knee @" not in out.read_text(encoding="utf-8")


def test_failed_and_thin_levels_keep_counts_without_latency_or_bridging():
    stats = aggregate([
        RequestRecord(session_id="s", concurrency=c, started_at=0, total_ms=100 * c, ok=ok)
        for c, successes in ((1, 10), (2, 0), (3, 9), (4, 10))
        for ok in [True] * successes + [False] * (100 - successes)
    ])
    knee = knee_figure(stats, None)
    assert list(knee.data[1].x) == [1, 2, 3, 4]
    assert list(knee.data[1].y) == [100, None, None, 400]
    assert knee.data[1].connectgaps is False
    ticks = knee.layout.xaxis.ticktext
    assert "successful=0 / attempts=100 - not plotted" in ticks[1]
    assert "successful=9 / attempts=100 - not plotted" in ticks[2]
    drift = drift_figure(stats, [LevelResolution(concurrency=2, judged=20, resolved_rate=0)])
    assert list(drift.data[0].y) == [100, None, None, 400]
    assert list(drift.data[1].y) == [0]
