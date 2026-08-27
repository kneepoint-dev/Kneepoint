from pydantic import BaseModel, Field

from kneepoint.chaos.faults import FaultSpec

# The JSONL is a public contract other people's tools read (docs/output-format.md).
# 0 means "written before versioning existed" — every file from before 2026-08-12,
# including all of Run D. Absent parses as 0 rather than failing.
SCHEMA_VERSION = 1


class VersionedRecord(BaseModel):
    """Version stamps carried by every line of every output file.

    Per record, not a header line: a JSONL consumer that tails, splits, greps or
    reads a partially-written file still sees the version on the line in front of
    it, and readers that predate this field ignore it instead of choking on a
    header that isn't a record. `JsonlWriter` stamps both on the way out.
    """

    schema_version: int = 0
    kneepoint_version: str | None = None


class RequestRecord(VersionedRecord):
    """One agent request attempt as observed by the load generator.

    A session is many turns, and a turn is 1..N attempts (retries). Every
    field carrying that structure has a default, so a JSONL file written
    before those fields existed — one line per single-turn request — still
    parses.
    """

    session_id: str
    concurrency: int          # ramp level this request ran at
    started_at: float         # unix epoch seconds
    ttft_ms: float | None = None
    total_ms: float
    input_tokens: int | None = None
    output_tokens: int | None = None
    ok: bool
    error: str | None = None
    turn_index: int = 0       # 0-based position within the session
    attempt: int = 1          # 1-based; >1 means this was a retry
    fault: str | None = None  # chaos fault injected into this attempt, if any
    status_code: int | None = None
    # Inter-token latency, at *chunk* granularity — a streamed chunk is not
    # guaranteed to be one token, so these are an approximation and are named as
    # one in the docs. Summary statistics only: a per-chunk timestamp array would
    # multiply the JSONL's size for a number nobody queries per chunk.
    # None (never 0) when the request produced fewer than two content chunks,
    # because one chunk defines no gap.
    itl_mean_ms: float | None = None
    itl_p99_ms: float | None = None
    chunk_count: int | None = None  # content-bearing chunks; usage-only chunks excluded
    # True when *we* gave up on an in-flight request (read-gap timeout or the
    # total wall) rather than the server answering. The upstream generation may
    # still be running, so latencies measured after this point are suspect —
    # LevelStats.contaminated carries that forward.
    abandoned: bool = False

    @property
    def tpot_ms(self) -> float | None:
        """Time per output token: `(total_ms - ttft_ms) / output_tokens`.

        Derived rather than stored — storing it would grow every line for a
        number that is a division away, and would let the stored value drift
        from the fields it came from. Anything that recomputes TPOT from a
        kneepoint record must use this exact expression and these exact guards;
        `tests/test_itl.py` pins it against an independent implementation.

        None when the request never produced content, when the provider sent no
        token usage, or when decode time isn't positive. Never 0.
        """
        if self.ttft_ms is None or not self.output_tokens:
            return None
        decode = self.total_ms - self.ttft_ms
        return decode / self.output_tokens if decode > 0 else None


class SessionRecord(VersionedRecord):
    """One simulated user session (a full multi-turn conversation)."""

    session_id: str
    concurrency: int
    started_at: float
    total_ms: float
    turns_requested: int
    turns_completed: int
    ok: bool                  # every turn eventually got a successful attempt
    faults: list[str] = Field(default_factory=list)
    transcript: list[dict] = Field(default_factory=list)  # {"role","content"}
    resolved: bool | None = None          # None = not judged
    resolution_method: str | None = None  # "deterministic" | "llm_judge"


class RampShape(BaseModel):
    start: int
    stop: int
    step: int


class TurnsShape(BaseModel):
    min: int
    max: int


class RetryShape(BaseModel):
    max_attempts: int
    backoff_s: float


class ChaosShape(BaseModel):
    """`profile` is the name the run was given; `faults` is what could actually fire.

    Stored in full rather than by name so a `custom` profile is reproducible from
    the file alone, and so a chaos run that happened to inject nothing is still
    distinguishable from `--chaos off` — the request lines cannot tell them apart.
    """

    profile: str
    faults: list[FaultSpec] = Field(default_factory=list)


class PriceRates(BaseModel):
    input_per_mtok: float
    output_per_mtok: float
    max_spend: float | None = None


class Environment(BaseModel):
    python: str
    platform: str


class RunMetadata(VersionedRecord):
    """The `run-<id>-meta.json` sidecar: everything the report needs that is not
    on a request or session line (docs/output-format.md).

    A sidecar rather than a JSONL header, because the JSONL promises there is no
    header line; a sidecar rather than per-record fields, because a target URL
    and a price table on every line is bloat. Absent (files written before this
    existed) means *unknown*, never a guess. `started_at`/`finished_at` are unix
    epoch seconds like the JSONL's `started_at`, and are the only timestamps —
    one representation, nothing to drift.
    """

    run_id: str
    command: str              # "run" | "demo"
    target: str
    model: str
    ramp: RampShape
    hold_seconds: float
    turns: TurnsShape
    retry: RetryShape
    seed: int
    chaos: ChaosShape
    price: PriceRates | None = None   # None = cost tracking was off
    min_samples: int
    min_group: int
    started_at: float         # taken before the first request is sent
    finished_at: float        # taken when this file is written
    environment: Environment
