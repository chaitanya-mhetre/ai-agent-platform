"""Eval suite format (YAML). One item = one scenario with deterministic checks."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

Category = Literal[
    "tool_selection",
    "incorrect_usage",
    "prompt_injection",
    "unsafe_actions",
    "hallucination",
    "failure_recovery",
]


class FailureInjection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool: str
    mode: Literal["transient_once", "timeout_once", "permanent", "garbage_output"]


class Expect(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tools_called: list[str] = Field(default_factory=list)  # must be proposed
    tools_not_called: list[str] = Field(default_factory=list)  # must NOT be proposed
    tools_not_executed: list[str] = Field(default_factory=list)  # may be proposed, never run
    final_contains: list[str] = Field(default_factory=list)
    final_not_contains: list[str] = Field(default_factory=list)
    status_in: list[str] = Field(default_factory=list)
    approval_requested_for: list[str] = Field(default_factory=list)
    notes_absent: list[str] = Field(default_factory=list)  # memory keys that must not exist


class Item(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    category: Category
    input: str
    description: str = ""
    user_permissions: list[str] = Field(default_factory=list)
    agent_tools: list[str] | None = None  # default: every registered tool
    inject_failure: FailureInjection | None = None
    attack: bool = False  # prompt_injection items: counts toward attack success rate
    expect: Expect


class Suite(BaseModel):
    name: str
    items: list[Item]
    sha: str


def load_suite(path: str | Path) -> Suite:
    p = Path(path)
    raw_text = p.read_text()
    raw = yaml.safe_load(raw_text)
    if "include" in raw:  # subset of another suite: {include: core.yaml, ids: [...]}
        parent = load_suite(p.parent / raw["include"])
        wanted = list(raw["ids"])
        by_id = {i.id: i for i in parent.items}
        missing = [i for i in wanted if i not in by_id]
        if missing:
            raise ValueError(f"unknown ids in {p.name}: {missing}")
        items = [by_id[i] for i in wanted]
        raw_text += parent.sha
    else:
        items = [Item.model_validate(x) for x in raw["items"]]
    ids = [i.id for i in items]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ValueError(f"duplicate item ids: {sorted(dupes)}")
    sha = hashlib.sha256(raw_text.encode()).hexdigest()[:12]
    return Suite(name=raw.get("name", p.stem), items=items, sha=sha)
