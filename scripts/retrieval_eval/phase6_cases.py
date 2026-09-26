"""Phase 6 case loading with the blind-confirmation split (new path).

The frozen Phase 1 file (``evals/cases/phase1_retrieval.json``) is never
edited: this module defines its own schema whose ``split`` additionally
accepts ``blind_confirmation`` and loads the Phase 6 development additions
(``evals/cases/phase6_dev_additions.json``) alongside it. Held-out cases
(HELD-01..20) are information only: never tune on them, never use them to
decide.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Phase6Split = Literal["development", "held_out", "blind_confirmation"]
Phase6Kind = Literal["integration", "retrieval_only"]

REPO = Path(__file__).parent.parent.parent
PHASE1_CASES = REPO / "evals" / "cases" / "phase1_retrieval.json"
PHASE6_DEV_ADDITIONS = REPO / "evals" / "cases" / "phase6_dev_additions.json"
RUBRIC = REPO / "evals" / "rubric_v1.md"


class Phase6Case(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(min_length=1)
    split: Phase6Split
    kind: Phase6Kind
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
    expect_hits: bool = False
    unsupported: list[str] = Field(default_factory=list)
    relevance_expectation: Literal["asserted", "not_asserted"] = "asserted"
    behavioral: list[str] = Field(default_factory=list)
    aggregate_group: str | None = None


class Phase6CaseFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str = Field(min_length=1)
    rubric_version: str = Field(min_length=1)
    split: Phase6Split = "development"
    cases: list[Phase6Case] = Field(min_length=1)


def load_phase6_file(path: str | Path) -> Phase6CaseFile:
    with open(path, encoding="utf-8") as handle:
        return Phase6CaseFile.model_validate(json.load(handle))


def check_phase6_file(data: Phase6CaseFile) -> list[str]:
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
    return problems


def eval_set_hash(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.read_bytes())
        digest.update(b"\x00")
    return digest.hexdigest()


def default_eval_files() -> list[Path]:
    files = [PHASE1_CASES, RUBRIC]
    if PHASE6_DEV_ADDITIONS.exists():
        files.insert(1, PHASE6_DEV_ADDITIONS)
    return files
