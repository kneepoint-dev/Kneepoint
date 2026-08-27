"""Fault vocabulary and profiles (docs/chaos.md); the v0 subset."""

from typing import Literal

from pydantic import BaseModel, Field

FaultType = Literal["llm_rate_limit", "llm_server_error", "tool_timeout", "tool_malformed_json"]

LLM_FAULTS: set[str] = {"llm_rate_limit", "llm_server_error"}
TOOL_FAULTS: set[str] = {"tool_timeout", "tool_malformed_json"}


class FaultSpec(BaseModel):
    type: FaultType
    probability: float = Field(ge=0.0, le=1.0)
    target: str = "*"  # tool-name glob; only meaningful for tool faults


STANDARD_PROFILE: list[FaultSpec] = [
    FaultSpec(type="llm_rate_limit", probability=0.02),
    FaultSpec(type="llm_server_error", probability=0.01),
    FaultSpec(type="tool_timeout", probability=0.05),
    FaultSpec(type="tool_malformed_json", probability=0.02),
]
