"""One-pass clock, throughput and closed-loop consistency analysis."""

from dataclasses import dataclass, field

from kneepoint.collect.schemas import RequestRecord

CLOCK_GAP_S = 1.0


@dataclass
class LevelTiming:
    concurrency: int
    source: str = "wall fallback"
    throughput_rps: float | None = None  # whole observed window, legacy/raw name
    steady_throughput_rps: float | None = None
    steady_completions: int = 0
    steady_mean_response_s: float | None = None
    little_gap_s: float | None = None
    suspected_stall: bool = False
    clock_anomaly: bool = False
    exact_clock_step: bool = False
    continuity_gap: bool = False
    max_continuity_gap_s: float = 0.0
    _rows: list[RequestRecord] = field(default_factory=list, repr=False)
    _wall_min: float = float("inf")
    _wall_max: float = float("-inf")
    _mono_min: float = float("inf")
    _mono_max: float = float("-inf")
    _offset_min: float = float("inf")
    _offset_max: float = float("-inf")
    _wall_end_max: float = float("-inf")
    _previous_session_end: dict[str, tuple[float, int, bool]] = field(
        default_factory=dict, repr=False,
    )
    _session_last_turn: dict[str, int] = field(default_factory=dict, repr=False)
    _max_pool_gap_s: float = float("-inf")
    _has_multiple_turns: bool = False
    _missing_monotonic: bool = False


def _inspect_record(
    level: LevelTiming, record: RequestRecord, max_think_s: float | None,
) -> None:
    """The sole per-record timing visit; kept separate so scale tests count it."""
    duration = record.total_ms / 1000
    wall_start = record.started_at
    wall_end = wall_start + duration
    level._wall_min = min(level._wall_min, wall_start)
    level._wall_max = max(level._wall_max, wall_end)
    if record.started_monotonic_s is None:
        level._missing_monotonic = True
    else:
        mono_start = record.started_monotonic_s
        level._mono_min = min(level._mono_min, mono_start)
        level._mono_max = max(level._mono_max, mono_start + duration)
        offset = wall_start - mono_start
        level._offset_min = min(level._offset_min, offset)
        level._offset_max = max(level._offset_max, offset)
    previous = level._previous_session_end.get(record.session_id)
    if (previous is not None and record.attempt == 1
            and record.turn_index > previous[1] and previous[2]
            and max_think_s is not None):
        gap = wall_start - previous[0] - max_think_s
        level.max_continuity_gap_s = max(level.max_continuity_gap_s, gap)
        if gap > CLOCK_GAP_S:
            level.continuity_gap = True
    # Old, one-turn files have no worker identity. In a continuously occupied
    # closed-loop pool, a gap after *all* known completions is still evidence of
    # a stopped client/host. The records are emitted in completion order.
    if level._wall_end_max != float("-inf") and record.attempt == 1:
        level._max_pool_gap_s = max(
            level._max_pool_gap_s, wall_start - level._wall_end_max,
        )
    level._wall_end_max = max(level._wall_end_max, wall_end)
    level._previous_session_end[record.session_id] = (
        wall_end, record.turn_index, record.ok,
    )
    level._session_last_turn[record.session_id] = max(
        record.turn_index, level._session_last_turn.get(record.session_id, 0),
    )
    level._has_multiple_turns |= record.turn_index > 0
    level._rows.append(record)


def analyze_timing(
    records: list[RequestRecord], *, hold_seconds: float | None = None,
    think_time_ms: tuple[int, int] | None = (0, 0),
    level_starts_monotonic_s: dict[int, float] | None = None,
    level_status: dict[int, str] | None = None,
) -> dict[int, LevelTiming]:
    """Analyze every attempt once; never sort records or scan them by level.

    The steady interval is [level start + hold/3, level start + 4*hold/5).
    The generator's recorded level start is authoritative on new runs. Legacy
    files use the first observed attempt as an approximate start and say so.
    """
    levels: dict[int, LevelTiming] = {}
    think_s = ((think_time_ms[0] + think_time_ms[1]) / 2000
               if think_time_ms is not None else None)
    max_think_s = think_time_ms[1] / 1000 if think_time_ms is not None else None
    for record in records:
        level = levels.setdefault(record.concurrency, LevelTiming(record.concurrency))
        _inspect_record(level, record, max_think_s)
    for level in levels.values():
        # Without metadata, a one-turn pool cannot be waiting on think time.
        # Multi-turn legacy files cannot bound that pause and are left unknown.
        pool_think_s = max_think_s if level._has_multiple_turns else 0.0
        if pool_think_s is not None and level._max_pool_gap_s != float("-inf"):
            gap = level._max_pool_gap_s - pool_think_s
            level.max_continuity_gap_s = max(level.max_continuity_gap_s, gap)
            level.continuity_gap |= gap > CLOCK_GAP_S
        level.exact_clock_step = (
            not level._missing_monotonic
            and level._offset_max - level._offset_min > CLOCK_GAP_S
        )
        level.clock_anomaly = level.exact_clock_step or level.continuity_gap
        monotonic = not level._missing_monotonic and level._mono_min != float("inf")
        level.source = "monotonic" if monotonic else "wall fallback"
        first = level._mono_min if monotonic else level._wall_min
        last = level._mono_max if monotonic else level._wall_max
        if last > first:
            level.throughput_rps = len(level._rows) / (last - first)
        if (hold_seconds is None or hold_seconds <= 0
                or level_status is not None
                and level_status.get(level.concurrency) != "completed"):
            continue
        level_start = (
            level_starts_monotonic_s[level.concurrency]
            if monotonic and level_starts_monotonic_s
            and level.concurrency in level_starts_monotonic_s else first
        )
        low = level_start + hold_seconds / 3
        high = level_start + hold_seconds * 4 / 5
        # Legacy sidecars may not carry a level outcome. If the last observed
        # completion precedes the end of the steady window, its denominator
        # includes time we cannot establish was measured.
        if level_status is None and last < high:
            continue
        response_total = 0.0
        think_events = 0
        for record in level._rows:
            start = record.started_monotonic_s if monotonic else record.started_at
            duration = record.total_ms / 1000
            if low <= start + duration < high:
                level.steady_completions += 1
                response_total += duration
                think_events += (record.ok and record.turn_index
                                 < level._session_last_turn[record.session_id])
        width = high - low
        level.steady_throughput_rps = level.steady_completions / width
        if level.steady_completions:
            mean_r = response_total / level.steady_completions
            level.steady_mean_response_s = mean_r
            if think_s is None:
                continue  # old metadata did not record Z; never invent a law gap
            # Only successful turns followed by another turn sleep. A worker
            # starts its next session immediately after the final turn.
            effective_think_s = think_s * think_events / level.steady_completions
            level.little_gap_s = (
                level.concurrency / level.steady_throughput_rps - mean_r - effective_think_s
            )
            level.suspected_stall = abs(level.little_gap_s) > max(0.050, 0.05 * mean_r)
    return levels
