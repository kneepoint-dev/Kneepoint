"""Seeded fault decisions, shared by the llm transport and the tool proxy."""

import random
from typing import Literal

from kneepoint.chaos.faults import LLM_FAULTS, FaultSpec


class ChaosInjector:
    def __init__(self, faults: list[FaultSpec], seed: int = 0) -> None:
        self.faults = faults
        self._rng = random.Random(seed)

    def pick(self, scope: Literal["llm", "tool"]) -> FaultSpec | None:
        """Roll each fault of the given scope independently; first hit wins."""
        for fault in self.faults:
            in_scope = (fault.type in LLM_FAULTS) == (scope == "llm")
            if in_scope and self._rng.random() < fault.probability:
                return fault
        return None
