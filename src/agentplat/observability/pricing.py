from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from agentplat.messages import Usage


@dataclass(frozen=True, slots=True)
class Price:
    input_per_mtok: float | None
    output_per_mtok: float | None


class PriceTable:
    def __init__(self, prices: dict[str, Price]) -> None:
        self.prices = prices

    @classmethod
    def load(cls, path: str | Path) -> PriceTable:
        p = Path(path)
        if not p.exists():
            return cls({})
        raw = yaml.safe_load(p.read_text()) or {}
        return cls(
            {
                name: Price(v.get("input_per_mtok"), v.get("output_per_mtok"))
                for name, v in (raw.get("models") or {}).items()
            }
        )

    def lookup(self, model: str) -> Price | None:
        if model in self.prices:
            return self.prices[model]
        matches = [k for k in self.prices if model.startswith(k)]
        return self.prices[max(matches, key=len)] if matches else None

    def cost(self, model: str, usage: Usage) -> float | None:
        """USD cost, or None when the price is unknown (never silently 0)."""
        price = self.lookup(model)
        if price is None or price.input_per_mtok is None or price.output_per_mtok is None:
            return None
        return (
            usage.prompt_tokens * price.input_per_mtok
            + usage.completion_tokens * price.output_per_mtok
        ) / 1_000_000
