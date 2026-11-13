"""Prompt corpus: seeded sampling over a real prompt distribution."""

import bisect
import glob
import json
import math
import random
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

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


class SamplingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    policy: Literal["uniform", "without_replacement", "zipf"] = "uniform"
    exponent: float = Field(1.0, gt=0, allow_inf_nan=False)
    task_weights: dict[str, float] | None = None

    @model_validator(mode="after")
    def weights_sum(self):
        if self.task_weights is not None:
            weights = list(self.task_weights.values())
            if (not weights or any(not math.isfinite(w) or w <= 0 for w in weights)
                    or not math.isclose(sum(weights), 1.0, abs_tol=1e-9)):
                raise ValueError("task weights must be positive, finite and sum to 1")
        return self


class CorpusSampler:
    """Seeded uniform, unique or Zipf draws from an explicitly ranked list.

    String inputs keep the original API. Dictionary items carry selection
    metadata. Optional task weights choose a class first, then sample inside it.
    Unique sampling fails when the chosen class is exhausted, without resampling.
    """

    def __init__(self, prompts: list, seed: int = 0, config: SamplingConfig | None = None):
        if not prompts:
            raise ValueError("prompt corpus is empty")
        self.config = config or SamplingConfig()
        self.items = [
            ({"prompt": item, "item_id": str(i + 1), "popularity_rank": i + 1,
              "template_id": "default", "length_class": "short", "off_topic": False}
             if isinstance(item, str) else dict(item)) for i, item in enumerate(prompts)
        ]
        if len({item["item_id"] for item in self.items}) != len(self.items):
            raise ValueError("corpus item IDs must be unique")
        for item in self.items:
            if not isinstance(item.get("prompt"), str) or not item["prompt"]:
                raise ValueError("every corpus item needs a nonempty prompt")
        self.prompts = [item["prompt"] for item in self.items]
        self._rng = random.Random(seed)
        self._seen: set[str] = set()
        weights = self.config.task_weights
        self._groups = ({name: [x for x in self.items if x.get("task_class") == name]
                         for name in weights} if weights else {"all": self.items[:]})
        if any(not items for items in self._groups.values()):
            raise ValueError("every weighted task class must have corpus items")
        self._cdf = {}
        for name, items in self._groups.items():
            if self.config.policy == "without_replacement":
                self._rng.shuffle(items)
            total = 0.0
            cdf = []
            for rank in range(1, len(items) + 1):
                total += rank ** -self.config.exponent
                cdf.append(total)
            self._cdf[name] = cdf

    @classmethod
    def from_glob(cls, pattern: str, seed: int = 0, config: SamplingConfig | None = None):
        prompts = []
        for p in sorted(glob.glob(pattern)):
            text = Path(p).read_text(encoding="utf-8").strip()
            if not text:
                continue
            # JSON corpus files contain an explicit ranked array of item objects.
            prompts.extend(json.loads(text) if p.endswith(".json") else [text])
        if not prompts:
            raise ValueError(f"corpus glob matched no non-empty files: {pattern!r}")
        return cls(prompts, seed=seed, config=config)

    def sample_item(self) -> dict:
        weights = self.config.task_weights
        name = (self._rng.choices(list(weights), weights=list(weights.values()))[0]
                if weights else "all")
        items = self._groups[name]
        if not items:
            raise CorpusExhausted(f"without_replacement exhausted class {name!r}")
        if self.config.policy == "without_replacement":
            return items.pop().copy()
        if self.config.policy == "zipf":
            cdf = self._cdf[name]
            return items[bisect.bisect_right(cdf, self._rng.random() * cdf[-1])].copy()
        return self._rng.choice(items).copy()

    def dispatched(self, item: dict) -> dict:
        selection = {k: v for k, v in item.items() if k != "prompt"}
        selection["first_use"] = item["item_id"] not in self._seen
        self._seen.add(item["item_id"])
        return selection

    def sample(self) -> str:
        return self.sample_item()["prompt"]


class CorpusExhausted(RuntimeError):
    """Every item has been dispatched; unique sampling must never wrap."""
