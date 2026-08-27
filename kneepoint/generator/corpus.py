"""Prompt corpus: seeded sampling over a real prompt distribution."""

import glob
import random
from pathlib import Path

DEFAULT_PROMPTS = [
    "Where is my order?",
    "I was charged twice for ticket 4471, please fix it.",
    "How do I reset my password? The reset email never arrives.",
    "Your app deleted my saved addresses after the last update and now checkout fails "
    "every time with error CK-209. What is going on?",
    "I need to change the shipping address on order 88213 before it ships tomorrow morning — "
    "the building number is wrong and the courier will not find it.",
    "Summarize my open support tickets, tell me which one is oldest, and explain what the "
    "next step is for each of them so I can decide whether to escalate anything to a human.",
]


class CorpusSampler:
    """Uniform, seeded sampling from a fixed prompt list."""

    def __init__(self, prompts: list[str], seed: int = 0) -> None:
        if not prompts:
            raise ValueError("prompt corpus is empty")
        self.prompts = prompts
        self._rng = random.Random(seed)

    @classmethod
    def from_glob(cls, pattern: str, seed: int = 0) -> "CorpusSampler":
        """One prompt per matched file (whole file, stripped)."""
        prompts = [
            text for p in sorted(glob.glob(pattern))
            if (text := Path(p).read_text(encoding="utf-8").strip())
        ]
        if not prompts:
            raise ValueError(f"corpus glob matched no non-empty files: {pattern!r}")
        return cls(prompts, seed=seed)

    def sample(self) -> str:
        return self._rng.choice(self.prompts)
