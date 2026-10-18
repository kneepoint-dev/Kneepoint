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
    _rows: list[tuple[float, float, float]] = field(default_factory=list, repr=False)
    _wall_min: float = float("inf")
    _wall_max: float = float("-inf")
    _mono_min: float = float("inf")
    _mono_max: float = float("-inf")
    _offset_min: float = float("inf")
    _offset_max: float = float("-inf")
    _wall_end_max: float = float("-inf")
    _previous_session_end: dict[str, float] = field(default_factory=dict, repr=False)
    _missing_monotonic: bool = False


def _inspect_record(
    level: LevelTiming, record: RequestRecord, max_think_s: float,
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
    if previous is not None and record.attempt == 1:
        gap = wall_start - previous - max_think_s
        level.max_continuity_gap_s = max(level.max_continuity_gap_s, gap)
        if gap > CLOCK_GAP_S:
            level.continuity_gap = True
    # Old, one-turn files have no worker identity. In a continuously occupied
    # closed-loop pool, a gap after *all* known completions is still evidence of
    # a stopped client/host. The records are emitted in completion order.
    if level._wall_end_max != float("-inf") and record.attempt == 1:
        gap = wall_start - level._wall_end_max - max_think_s
        level.max_continuity_gap_s = max(level.max_continuity_gap_s, gap)
        if gap > CLOCK_GAP_S:
            level.continuity_gap = True
    level._wall_end_max = max(level._wall_end_max, wall_end)
    level._previous_session_end[record.session_id] = wall_end
    level._rows.append((wall_start, record.started_monotonic_s or 0.0, duration))


def analyze_timing(
    records: list[RequestRecord], *, hold_seconds: float | None = None,
    think_time_ms: tuple[int, int] | None = (0, 0),
    level_starts_monotonic_s: dict[int, float] | None = None,
) -> dict[int, LevelTiming]:
    """Analyze every attempt once; never sort records or scan them by level.

    The steady interval is [level start + hold/3, level start + 4*hold/5).
    The generator's recorded level start is authoritative on new runs. Legacy
    files use the first observed attempt as an approximate start and say so.
    """
    levels: dict[int, LevelTiming] = {}
    think_s = ((think_time_ms[0] + think_time_ms[1]) / 2000
               if think_time_ms is not None else None)
    max_think_s = think_time_ms[1] / 1000 if think_time_ms is not None else 0.0
    for record in records:
        level = levels.setdefault(record.concurrency, LevelTiming(record.concurrency))
        _inspect_record(level, record, max_think_s)
    for level in levels.values():
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
        if hold_seconds is None or hold_seconds <= 0:
            continue
        level_start = (
            level_starts_monotonic_s[level.concurrency]
            if monotonic and level_starts_monotonic_s
            and level.concurrency in level_starts_monotonic_s else first
        )
        low = level_start + hold_seconds / 3
        high = level_start + hold_seconds * 4 / 5
        response_total = 0.0
        for wall_start, mono_start, duration in level._rows:
            start = mono_start if monotonic else wall_start
            if low <= start + duration < high:
                level.steady_completions += 1
                response_total += duration
        width = high - low
        level.steady_throughput_rps = level.steady_completions / width
        if level.steady_completions:
            mean_r = response_total / level.steady_completions
            level.steady_mean_response_s = mean_r
            if think_s is None:
                continue  # old metadata did not record Z; never invent a law gap
            level.little_gap_s = (
                level.concurrency / level.steady_throughput_rps - mean_r - think_s
            )
            level.suspected_stall = abs(level.little_gap_s) > max(0.050, 0.05 * mean_r)
    return levels
