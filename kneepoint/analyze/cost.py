"""Cost engine: token pricing -> $/resolved task and retry-waste % (pillar #2)."""

from pydantic import BaseModel

from kneepoint.collect.schemas import RequestRecord, SessionRecord

_MTOK = 1_000_000


class CostRates(BaseModel):
    """USD per million tokens, from the scenario's `cost` block (docs/scenarios.md)."""

    input_per_mtok: float = 0.0
    output_per_mtok: float = 0.0


class CostSummary(BaseModel):
    total_spend: float
    waste_spend: float
    retry_waste_pct: float          # waste / total, 0 when total is 0
    sessions: int
    judged_sessions: int
    resolved_sessions: int
    cost_per_session: float | None  # None when there are no sessions
    cost_per_resolved: float | None  # THE metric; None when nothing resolved


def request_cost(record: RequestRecord, rates: CostRates) -> float:
    return (
        (record.input_tokens or 0) * rates.input_per_mtok
        + (record.output_tokens or 0) * rates.output_per_mtok
    ) / _MTOK


def summarize_cost(
    records: list[RequestRecord],
    sessions: list[SessionRecord],
    rates: CostRates,
) -> CostSummary:
    total = sum(request_cost(r, rates) for r in records)
    waste = sum(request_cost(r, rates) for r in records if not r.ok)
    judged = [s for s in sessions if s.resolved is not None]
    resolved = sum(1 for s in judged if s.resolved)
    return CostSummary(
        total_spend=total,
        waste_spend=waste,
        retry_waste_pct=waste / total if total > 0 else 0.0,
        sessions=len(sessions),
        judged_sessions=len(judged),
        resolved_sessions=resolved,
        cost_per_session=total / len(sessions) if sessions else None,
        cost_per_resolved=total / resolved if resolved else None,
    )
