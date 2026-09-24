"""Typed schema for Phase 1 retrieval evaluation cases.

Behavioral expectations (``expect_hits``, ``behavioral``) stay separate
from relevance judgments: ``relevance_expectation`` records whether a
topical relevance claim is even asserted for the case. Relevance labels
themselves live in the recorded review layer, never in the case file.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Split = Literal["development", "held_out"]
Kind = Literal["integration", "retrieval_only"]


class Phase1Case(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(min_length=1)
    split: Split
    kind: Kind
    category: str = Field(min_length=1)
    dish: str | None = None
    ingredients: list[str] = Field(default_factory=list)
    dietary_constraints: list[str] = Field(default_factory=list)
    equipment: list[str] = Field(default_factory=list)
    time_minutes: float | int | None = None
    query_text: str | None = None
    ingredients_filter: Any | None = None
    dataset_id: str | None = None
    limit: int = Field(default=5, ge=1, le=50)
    notes: str = ""
    expect_hits: bool
    unsupported: list[str] = Field(default_factory=list)
    relevance_expectation: Literal["asserted", "not_asserted"] = "asserted"
    behavioral: list[str] = Field(default_factory=list)
    aggregate_group: str | None = None


class Phase1CaseFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str = Field(min_length=1)
    rubric_version: str = Field(min_length=1)
    frozen_split: dict[str, int] = Field(default_factory=dict)
    split_policy: str = ""
    kind_policy: str = ""
    label_status: str = ""
    default_limit: int = 5
    supersedes: str | None = None
    changelog: list[str] = Field(default_factory=list)
    aggregation_notes: dict[str, Any] = Field(default_factory=dict)
    cases: list[Phase1Case] = Field(min_length=1)


def load_case_file(path: str) -> Phase1CaseFile:
    import json

    with open(path, encoding="utf-8") as handle:
        return Phase1CaseFile.model_validate(json.load(handle))


def check_case_file(data: Phase1CaseFile) -> list[str]:
    """Cross-case consistency checks; returns human-readable problems."""
    problems: list[str] = []
    seen: set[str] = set()
    for case in data.cases:
        if case.case_id in seen:
            problems.append(f"duplicate case_id {case.case_id}")
        seen.add(case.case_id)
        if case.kind == "integration" and not (case.dish or case.ingredients):
            problems.append(f"{case.case_id}: integration case needs dish or ingredients")
        if case.kind == "retrieval_only" and not case.query_text:
            problems.append(f"{case.case_id}: retrieval_only case needs query_text")
        if case.time_minutes is not None and not 1 <= float(case.time_minutes) <= 1440:
            problems.append(f"{case.case_id}: time_minutes out of range")
    dev = sum(1 for c in data.cases if c.split == "development")
    held = sum(1 for c in data.cases if c.split == "held_out")
    frozen = data.frozen_split
    if frozen.get("development") != dev or frozen.get("held_out") != held:
        problems.append(f"frozen_split {frozen} does not match actual dev={dev} held_out={held}")
    return problems
