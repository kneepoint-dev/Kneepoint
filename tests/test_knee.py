from kneepoint.analyze.knee import (
    KneeResult,
    LevelStats,
    aggregate,
    find_knee,
    knee_report,
)
from kneepoint.collect.schemas import RequestRecord


def _rec(level: int, ms: float, ok: bool = True) -> RequestRecord:
    return RequestRecord(
        session_id="s", concurrency=level, started_at=0.0,
        ttft_ms=ms / 2 if ok else None, total_ms=ms, ok=ok,
        error=None if ok else "boom",
    )


def _stats(p95_by_level: dict[int, float], n: int = 50) -> list[LevelStats]:
    return [
        LevelStats(concurrency=c, n=n, error_rate=0.0,
                   p50_ms=v * 0.6, p95_ms=v, p99_ms=v * 1.2)
        for c, v in sorted(p95_by_level.items())
    ]


def test_aggregate_groups_and_computes_percentiles():
    records = [
        _rec(1, 100.0), _rec(1, 200.0), _rec(1, 300.0),
        _rec(2, 400.0, ok=False), _rec(2, 500.0),
    ]
    stats = aggregate(records)
    assert [s.concurrency for s in stats] == [1, 2]
    assert stats[0].n == 3
    assert stats[0].p50_ms == 200.0
    assert stats[1].error_rate == 0.5


def test_aggregate_drops_levels_with_no_successes():
    stats = aggregate([_rec(1, 100.0), _rec(2, 400.0, ok=False)])
    assert [s.concurrency for s in stats] == [1]


def test_find_knee_on_hockey_stick():
    p95 = {c: 1000.0 for c in range(1, 6)} | {c: 1000.0 + (c - 5) * 800.0 for c in range(6, 11)}
    knee = find_knee(_stats(p95))
    assert isinstance(knee, KneeResult)
    assert 5 <= knee.concurrency <= 7


def test_find_knee_on_linear_data_returns_none():
    p95 = {c: 1000.0 + 100.0 * c for c in range(1, 11)}
    assert find_knee(_stats(p95)) is None


def test_find_knee_ignores_thin_levels():
    p95 = {c: 1000.0 for c in range(1, 6)} | {c: 1000.0 + (c - 5) * 800.0 for c in range(6, 11)}
    assert find_knee(_stats(p95, n=3), min_samples=10) is None


# ---------------------------------------------------------------------------
# knee_report: both methods, the deciding ratios, and a confidence signal
#
# Run D: three repetitions of one scenario gave knees 3/2/2 because the deciding
# ratios were 1.85 / 2.48 / 2.63 around a hard 2.0 threshold, and a fourth run
# printed Kneedle's 5 where p95-doubling said 2 without saying a word about it.
# ---------------------------------------------------------------------------


def test_both_methods_are_always_reported():
    report = knee_report(_stats({1: 100.0, 2: 105.0, 3: 110.0, 4: 400.0, 5: 800.0, 6: 1600.0}))
    assert report.kneedle.method == "kneedle"
    assert report.p95_doubling.method == "p95_doubling"
    assert report.kneedle.concurrency is not None
    assert report.p95_doubling.concurrency is not None
    assert report.agree is True


def test_disagreement_is_stated_but_kneedle_never_takes_the_headline():
    """Kneedle answered on 7 of Run D's 15 repetitions and read higher on every
    one, so the old kneedle-first CLI headline contradicted every figure the
    project publishes. It corroborates the headline or it doesn't; it never
    becomes it."""
    report = knee_report(_stats({1: 100.0, 2: 102.0, 3: 104.0, 4: 500.0, 5: 900.0, 6: 1400.0}))
    assert report.agree is False
    assert report.kneedle.concurrency == 3
    assert report.knee == 4                # p95-doubling's, not Kneedle's
    assert report.knee_range == (4, 4)     # disagreement lowers confidence, not the range
    assert report.confidence == "low"
    assert any("kneedle disagrees" in note for note in report.notes)
    # and both numbers survive into what the user is shown
    text = "\n".join(report.lines())
    assert "kneedle: 3" in text and "p95_doubling: 4" in text


def test_a_ratio_near_the_threshold_makes_the_knee_a_range():
    """gemma4 rep1: p95 at c=2 was 1.85x the floor. A repetition lands the other
    side of 2.0 and the answer moves - so the answer is 2-3, not 3."""
    report = knee_report(_stats({1: 100.0, 2: 190.0, 3: 400.0, 4: 800.0, 5: 1500.0}))
    assert report.p95_doubling.concurrency == 3
    assert report.knee_range == (2, 3)
    assert report.confidence == "low"
    assert report.headline == (
        "Knee point: 2-3 concurrent sessions, low confidence (plan for 2)"
    )
    assert report.recommended == 2  # a capacity number errs low
    assert [r.near_threshold for r in report.ratios] == [False, True, False, False, False]


def test_a_ratio_that_barely_crossed_extends_the_range_upward():
    """The crossing itself can be the coin flip: at 2.05x, the next repetition
    may not cross here at all and the level above becomes the knee."""
    report = knee_report(_stats({1: 100.0, 2: 120.0, 3: 205.0, 4: 600.0, 5: 1200.0}))
    assert report.p95_doubling.concurrency == 3
    assert report.knee_range[1] >= 4
    assert report.confidence == "low"
    assert any("within 15% of the 2.0 threshold" in note for note in report.notes)


def test_a_clean_crossing_keeps_its_single_number():
    """Confidence has to be earnable, or the signal means nothing."""
    report = knee_report(_stats({1: 100.0, 2: 105.0, 3: 110.0, 4: 400.0, 5: 800.0, 6: 1600.0}))
    assert report.knee == 4
    assert report.knee_range == (4, 4)
    assert report.confidence == "high"
    assert "high confidence" in report.headline


def test_kneedle_is_refused_when_it_lands_on_the_top_of_the_ramp():
    """The nemotron Run D scout, verbatim (p95 as a multiple of the c=1 floor):
    Kneedle answered 16 on a 1..16 ramp. That is the ramp's own upper bound, not
    a measurement of the system - the same curve doubles its p95 at c=2."""
    curve = [1.00, 2.15, 2.77, 4.32, 4.19, 5.04, 6.37, 6.54,
             7.49, 7.18, 10.79, 9.37, 11.04, 10.62, 13.02, 12.71]
    p95 = {c: v * 1000.0 for c, v in enumerate(curve, start=1)}
    report = knee_report(_stats(p95))
    assert report.kneedle.concurrency is None
    assert "top of the ramp" in report.kneedle.note
    assert report.p95_doubling.concurrency == 2
    assert find_knee(_stats(p95)).concurrency == 2  # not 16, as it was before


def test_doubling_at_the_top_of_the_ramp_is_a_lower_bound_not_a_knee():
    """The `kneepoint demo` case on a 1..13 ramp: p95 first reached 2x at 13,
    the last level, and the headline read "knee at 13, high confidence". Every
    level below stayed under 2x - that much is measured - but nothing above 13
    exists to show a break rather than a gentle climb that happened to reach
    2x. Same non-answer Kneedle is refused for, now on the method that leads."""
    p95 = {1: 100.0, 2: 110.0, 3: 125.0, 4: 145.0, 5: 170.0, 6: 210.0}
    report = knee_report(_stats(p95))
    assert report.p95_doubling.concurrency == 6
    assert "top of the ramp" in report.p95_doubling.note
    assert report.knee == 6
    assert report.lower_bound is True
    assert report.confidence == "low"
    assert "high confidence" not in report.headline
    assert report.headline.startswith("Knee point: 6 concurrent sessions or beyond, low confidence")
    assert "widen it past 6" in report.headline
    assert any("lower bound" in note for note in report.notes)
    assert report.recommended == 6  # "not below 6" is still the number to size from
    assert find_knee(_stats(p95)).concurrency == 6


def test_a_near_threshold_level_below_a_top_of_ramp_crossing_still_widens_the_range():
    """1.9x at c=5 then 2.4x at c=6, the top: a repetition could cross at 5, so
    the range is 5-6 - and it is still "or beyond", because 6 is the last level."""
    report = knee_report(_stats({1: 100.0, 2: 110.0, 3: 125.0, 4: 145.0, 5: 190.0, 6: 240.0}))
    assert report.knee_range == (5, 6)
    assert report.lower_bound is True
    assert report.headline.startswith(
        "Knee point: 5-6 concurrent sessions or beyond, low confidence")
    assert "plan for 5" in report.headline
    assert report.recommended == 5


def test_a_crossing_below_the_top_is_not_marked_as_a_bound():
    """The guard must not fire on a real mid-ramp knee, or it would downgrade
    every clean answer to low confidence and the signal would mean nothing."""
    report = knee_report(_stats({1: 100.0, 2: 105.0, 3: 110.0, 4: 400.0, 5: 800.0, 6: 1600.0}))
    assert report.lower_bound is False
    assert report.p95_doubling.note is None
    assert report.confidence == "high"
    assert "or beyond" not in report.headline


def test_kneedle_alone_is_not_a_knee():
    """The `kneepoint demo` case: Kneedle picks a level on a curve whose p95
    never doubles. p95-doubling is the definition, so its silence means nothing
    broke — Kneedle's curvature gets said out loud and scored as no knee."""
    report = knee_report(_stats({1: 100.0, 2: 101.0, 3: 101.0, 4: 128.0, 5: 129.0}))
    assert report.p95_doubling.concurrency is None
    assert report.kneedle.concurrency == 3
    assert report.knee is None
    assert report.recommended is None
    assert report.confidence is None
    assert any("kneedle sees curvature at c=3" in n for n in report.notes)
    # and it does not also claim "neither method finds a knee"
    assert not any("neither method" in n for n in report.notes)


def test_kneedle_abstaining_does_not_weaken_a_clean_doubling_knee():
    """The reverse is not symmetric: Kneedle abstains on near-linear curves by
    construction, and 13 of Run D's 15 repetitions are near-linear. Penalising
    p95-doubling for that would make every real measurement low confidence."""
    report = knee_report(_stats({1: 100.0, 2: 148.0, 3: 300.0, 4: 330.0, 5: 380.0}))
    assert report.kneedle.concurrency is None
    assert report.p95_doubling.concurrency == 3
    assert report.confidence == "high"


def test_no_knee_invents_no_confidence():
    p95 = {c: 1000.0 + 100.0 * c for c in range(1, 11)}
    report = knee_report(_stats(p95))
    assert report.knee is None
    assert report.knee_range is None
    assert report.confidence is None
    assert report.headline == "No clear knee found."
    assert any("never doubles" in note for note in [report.p95_doubling.note or ""])


def test_thin_levels_produce_a_reason_not_a_guess():
    report = knee_report(_stats({1: 100.0, 2: 500.0, 3: 900.0}, n=3), min_samples=10)
    assert report.knee is None
    assert report.usable_levels == 0
    assert report.ratios == []
    assert "no knee is claimed" in report.notes[0]


def test_contaminated_levels_cannot_carry_high_confidence():
    """A knee measured on levels poisoned by abandoned work is not a knee,
    however clean the curve looks (METHODOLOGY §4b)."""
    stats = _stats({1: 100.0, 2: 105.0, 3: 110.0, 4: 400.0, 5: 800.0, 6: 1600.0})
    for s in stats[3:]:
        s.contaminated = True
    report = knee_report(stats)
    assert report.confidence == "low"
    assert report.contaminated_levels == [4, 5, 6]
    assert any("not publishable" in note for note in report.notes)


def test_ratios_are_exposed_so_the_verdict_can_be_argued_with():
    report = knee_report(_stats({1: 200.0, 2: 400.0, 3: 900.0}))
    assert [round(r.ratio, 2) for r in report.ratios] == [1.0, 2.0, 4.5]
    assert "p95 vs curve floor" in "\n".join(report.lines())


def test_find_knee_still_answers_for_callers_that_need_one_integer():
    report = knee_report(_stats({1: 100.0, 2: 105.0, 3: 110.0, 4: 400.0, 5: 800.0, 6: 1600.0}))
    knee = find_knee(_stats({1: 100.0, 2: 105.0, 3: 110.0, 4: 400.0, 5: 800.0, 6: 1600.0}))
    assert isinstance(knee, KneeResult)
    assert knee.concurrency == report.knee


def test_find_knee_is_always_p95_doubling_now():
    """Kneedle used to win whenever it answered, which is how the CLI headline
    ended up contradicting kneepoint's own stated knee definition on 7 of Run D's
    15 repetitions."""
    stats = _stats({1: 100.0, 2: 102.0, 3: 104.0, 4: 500.0, 5: 900.0, 6: 1400.0})
    assert knee_report(stats).kneedle.concurrency == 3
    knee = find_knee(stats)
    assert knee.method == "p95_doubling"
    assert knee.concurrency == 4


def test_find_knee_returns_the_conservative_end_of_the_range():
    """Anything asking for one integer is sizing something, and a capacity
    number errs low: under the knee costs headroom, over it costs everyone."""
    stats = _stats({1: 100.0, 2: 190.0, 3: 400.0, 4: 800.0, 5: 1500.0})
    report = knee_report(stats)
    assert report.knee == 3 and report.knee_range == (2, 3)
    assert find_knee(stats).concurrency == 2


def test_find_knee_is_none_when_only_kneedle_answered():
    assert find_knee(_stats({1: 100.0, 2: 101.0, 3: 101.0, 4: 128.0, 5: 129.0})) is None
