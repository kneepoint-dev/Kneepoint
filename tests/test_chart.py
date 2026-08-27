from kneepoint.analyze.knee import KneeResult, LevelStats
from kneepoint.report.chart import write_knee_chart


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
