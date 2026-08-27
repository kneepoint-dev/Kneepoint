"""LLM-as-judge on a sampled subset of session transcripts.

Judging costs real money on real APIs: sample, don't exhaustively judge, and
never let a judge failure kill a load-test run.
"""

import json
import math
import os
import random
import re

import httpx
from pydantic import BaseModel, Field

from kneepoint.collect.schemas import SessionRecord

DEFAULT_RUBRIC = (
    "You judge customer-support agent conversations. Decide whether the user's task "
    "was RESOLVED by the final state of the conversation: the agent gave a concrete, "
    "correct-looking answer or completed the requested action. Vague deflection, "
    "errors, or unanswered questions mean not resolved."
)

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


class JudgeConfig(BaseModel):
    base_url: str
    model: str
    api_key_env: str = "KNEEPOINT_JUDGE_API_KEY"
    sample_rate: float = Field(0.2, gt=0, le=1)
    rubric: str = DEFAULT_RUBRIC
    timeout_s: float = 30.0


def _judge_messages(cfg: JudgeConfig, session: SessionRecord) -> list[dict]:
    transcript = "\n".join(f"{m['role']}: {m['content']}" for m in session.transcript)
    return [
        {"role": "system", "content": cfg.rubric},
        {
            "role": "user",
            "content": (
                "Conversation:\n---\n" + transcript + "\n---\n"
                'Reply with ONLY this JSON: {"resolved": true|false, "reason": "<short>"}'
            ),
        },
    ]


def _parse_verdict(text: str) -> bool | None:
    match = _JSON_RE.search(text)
    if not match:
        return None
    try:
        verdict = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    resolved = verdict.get("resolved")
    return resolved if isinstance(resolved, bool) else None


async def judge_sessions(
    sessions: list[SessionRecord],
    cfg: JudgeConfig,
    *,
    seed: int = 0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> int:
    """Judge a seeded sample of sessions in place; return how many got a verdict."""
    candidates = [s for s in sessions if s.transcript]
    if not candidates:
        return 0
    k = min(len(candidates), math.ceil(len(candidates) * cfg.sample_rate))
    picked = random.Random(seed).sample(candidates, k)
    headers = {}
    api_key = os.getenv(cfg.api_key_env)
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    judged = 0
    async with httpx.AsyncClient(
        timeout=cfg.timeout_s, transport=transport, headers=headers
    ) as client:
        for session in picked:
            try:
                resp = await client.post(
                    f"{cfg.base_url.rstrip('/')}/chat/completions",
                    json={"model": cfg.model, "messages": _judge_messages(cfg, session)},
                )
                resp.raise_for_status()
                text = resp.json()["choices"][0]["message"]["content"]
            except Exception:  # noqa: BLE001 - a judge outage must not kill the run
                continue
            verdict = _parse_verdict(text)
            if verdict is None:
                continue
            session.resolved = verdict
            session.resolution_method = "llm_judge"
            judged += 1
    return judged
