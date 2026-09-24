"""Build and validate the Phase 3 source-level review packet (read-only).

Usage:
  uv run python scripts/recommendations_live/review_packet.py check-spec
  uv run python scripts/recommendations_live/review_packet.py build
  uv run python scripts/recommendations_live/review_packet.py validate

``check-spec`` is offline: it checks the tracked spec files only. ``build``
and ``validate`` also read saved live artifacts and the stored source
documents through a read-only database connection (no writes, no provider
calls, no new searches).

Tracked inputs: ``evals/phase3_review/examples.json`` (reviewer judgments and
references) and ``evals/phase3_review/enrichment_proposals.json`` (proposals
with evidence references and excerpt hashes, no source text).

Outputs under ``evals/results/phase3_review/``: ``packet.json``/``packet.md``
and ``enrichment_fixtures.json``/``enrichment_fixtures.md`` hold source text
and saved model responses, so they stay out of Git (see ``.gitignore``);
``manifest.json`` holds identities and hashes only and is meant to be
versioned. Outputs carry no timestamps, so a rebuild from unchanged inputs
is byte-identical; ``validate`` rebuilds in memory and compares.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SPEC_DIR = ROOT / "evals" / "phase3_review"
EXAMPLES = SPEC_DIR / "examples.json"
PROPOSALS = SPEC_DIR / "enrichment_proposals.json"
OUT_DIR = ROOT / "evals" / "results" / "phase3_review"
OUTPUT_NAMES = ("packet.json", "packet.md", "enrichment_fixtures.json", "enrichment_fixtures.md")

ATTRIBUTION = (
    "AI-assisted review; pending owner acceptance; not independently "
    "human-verified or culinarily validated."
)
JUDGMENT_KEYS = ("execution", "fidelity", "usefulness", "constraint_support")
REQUIRED_CATEGORIES = {
    "useful_ordinary_recommendation",
    "repaired_native_tool_recommendation",
    "followup01_ingredient_list_inconsistency",
    "partial_quantities_unknown_units",
    "dietary_constrained_abstention",
    "relevance_usefulness_failure",
}
PROPOSAL_KINDS = {"source_backed_correction", "unresolved_must_remain_unknown", "source_defect"}
# Proposals never invent facts: these keys may not appear in a proposed change.
FORBIDDEN_CHANGE_KEYS = {"amount", "unit", "quantity_text", "servings", "total_minutes", "yield"}
SOURCE_SECTION_KEYS = (
    "title",
    "servings",
    "servings_text",
    "durations_minutes",
    "durations_reported",
    "flags",
    "quality_issues",
    "capabilities",
    "unresolved_fields",
    "notes_text",
)
INGREDIENT_KEYS = (
    "original",
    "canonical",
    "name",
    "amount",
    "amount_text",
    "quantity_text",
    "unit",
    "unit_text",
    "notes",
    "optional",
)


def dumps(payload: Any) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False) + "\n"


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_json(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return sha256_text(encoded)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def runner_fidelity_fingerprint(dataset_id: str, source_id: str, content_hash: Any) -> str:
    """Same encoding as ``runner._sha`` for the runner's fidelity record."""
    payload = {"dataset_id": dataset_id, "source_id": source_id, "content_hash": content_hash}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


# ------------------------------------------------------------------ spec ---


def check_spec(examples: dict[str, Any], proposals: dict[str, Any]) -> list[str]:
    """Structural checks on the tracked spec files (offline, no DB)."""
    errors: list[str] = []
    for name, spec in (("examples", examples), ("proposals", proposals)):
        if spec.get("attribution") != ATTRIBUTION:
            errors.append(f"{name}: attribution must be exactly {ATTRIBUTION!r}")
    if examples.get("review_status") != "pending_owner_acceptance":
        errors.append("examples: review_status must stay pending_owner_acceptance")
    contracts = set(examples.get("contract_versions") or {})
    items = examples.get("examples") or []
    ids = [e.get("id") for e in items]
    if len(ids) != len(set(ids)):
        errors.append("examples: duplicate ids")
    if not 5 <= len(items) <= 8:
        errors.append(f"examples: expected about six examples, got {len(items)}")
    covered: set[str] = set()
    proposal_ids = {p.get("id") for p in proposals.get("proposals") or []}
    for example in items:
        eid = example.get("id")
        covered.update(example.get("categories") or [])
        for key in ("artifact", "case_file", "case_id", "contract_version", "not_recorded"):
            if key not in example:
                errors.append(f"{eid}: missing {key}")
        if example.get("contract_version") not in contracts:
            errors.append(f"{eid}: unknown contract_version")
        judgments = example.get("judgments") or {}
        for key in JUDGMENT_KEYS:
            judgment = judgments.get(key) or {}
            if not judgment.get("verdict") or not judgment.get("rationale"):
                errors.append(f"{eid}: judgment {key} needs verdict and rationale")
        expected = example.get("expected_backend_behavior") or {}
        if not expected.get("historical_observed") or not expected.get("current_policy"):
            errors.append(f"{eid}: expected_backend_behavior needs historical and current")
        identity = example.get("identity")
        if identity is not None and set(identity) != {"dataset_id", "source_id"}:
            errors.append(f"{eid}: identity must be exact (dataset_id, source_id)")
        for fixture in example.get("linked_fixtures") or []:
            if fixture not in proposal_ids:
                errors.append(f"{eid}: unknown linked fixture {fixture}")
    missing = REQUIRED_CATEGORIES - covered
    if missing:
        errors.append(f"examples: categories not covered: {sorted(missing)}")
    kinds: set[str] = set()
    for proposal in proposals.get("proposals") or []:
        pid = proposal.get("id")
        kinds.add(str(proposal.get("kind")))
        if proposal.get("kind") not in PROPOSAL_KINDS:
            errors.append(f"{pid}: unknown kind")
        if proposal.get("status") != "proposed" or proposal.get("owner_decision") != "pending":
            errors.append(f"{pid}: must stay status=proposed, owner_decision=pending")
        identity = proposal.get("identity") or {}
        if set(identity) != {"dataset_id", "source_id"}:
            errors.append(f"{pid}: identity must be exact (dataset_id, source_id)")
        fingerprint = proposal.get("source_fingerprint") or {}
        if not fingerprint.get("stored_document_sha256"):
            errors.append(f"{pid}: missing source fingerprint")
        change = proposal.get("proposed_change")
        if proposal.get("kind") == "unresolved_must_remain_unknown":
            if change is not None:
                errors.append(f"{pid}: an unresolved issue proposes no change")
        elif not isinstance(change, dict):
            errors.append(f"{pid}: missing proposed_change")
        elif FORBIDDEN_CHANGE_KEYS & set(change):
            forbidden = sorted(FORBIDDEN_CHANGE_KEYS & set(change))
            errors.append(f"{pid}: proposed change may not set {forbidden}")
        if not proposal.get("abstentions"):
            errors.append(f"{pid}: abstentions must be explicit")
        for key in ("establishes", "does_not_establish", "evidence", "scope", "reviewer"):
            if not proposal.get(key):
                errors.append(f"{pid}: missing {key}")
        for ref in proposal.get("evidence") or []:
            if not ref.get("sha256") or not ref.get("section"):
                errors.append(f"{pid}: evidence refs need section and sha256")
    if kinds != PROPOSAL_KINDS:
        errors.append(f"proposals: kinds must cover {sorted(PROPOSAL_KINDS)}")
    return errors


# --------------------------------------------------------------- sources ---


def fetch_documents(identities: set[tuple[str, str]]) -> dict[tuple[str, str], dict[str, Any]]:
    """Exact-pair fetches through the production lookup, read-only."""
    from culinary_copilot.config import Settings
    from culinary_copilot.db import create_db_engine
    from culinary_copilot.recipes.repository import get_recipe

    engine = create_db_engine(Settings()).execution_options(postgresql_readonly=True)
    try:
        docs: dict[tuple[str, str], dict[str, Any]] = {}
        for dataset_id, source_id in sorted(identities):
            doc = get_recipe(engine, source_id, dataset_id=dataset_id)
            if doc is None:
                raise SystemExit(f"source not found: {dataset_id}/{source_id}")
            docs[(dataset_id, source_id)] = doc
        return docs
    finally:
        engine.dispose()


def resolve_excerpt(doc: dict[str, Any], ref: dict[str, Any]) -> Any:
    """Exact stored value for an evidence reference (verbatim, unnormalized)."""
    section = ref["section"]
    if section == "ingredients":
        index = int(str(ref["ref"]).removeprefix("ing-"))
        return doc["ingredients"][index].get(ref["key"])
    if section == "instructions":
        return doc["instructions"][int(str(ref["ref"]).removeprefix("step-"))]
    if section == "raw":
        return (doc.get("raw") or {}).get(ref["key"])
    if section == "flags":
        return doc.get("flags")
    if section == "ingredient_names":
        return [i.get("original") for i in doc.get("ingredients", [])]
    raise ValueError(f"unknown evidence section {section!r}")


def excerpt_sha256(value: Any) -> str:
    return sha256_text(value) if isinstance(value, str) else sha256_json(value)


def source_block(dataset_id: str, source_id: str, doc: dict[str, Any]) -> dict[str, Any]:
    from culinary_copilot.recommendations.evidence import build_candidate, evidence_fingerprint

    candidate = build_candidate(dataset_id=dataset_id, source_id=source_id, title=None, doc=doc)
    sections: dict[str, Any] = {k: doc[k] for k in SOURCE_SECTION_KEYS if k in doc}
    sections["ingredients"] = [
        {"ref": f"ing-{i}", **{k: item.get(k) for k in INGREDIENT_KEYS if k in item}}
        for i, item in enumerate(doc.get("ingredients", []))
    ]
    sections["instructions"] = [
        {"ref": f"step-{i}", "text": step} for i, step in enumerate(doc.get("instructions", []))
    ]
    if "quality_issues" not in doc:
        sections["quality_issues"] = "absent (row predates quality metadata)"
    shown = set(SOURCE_SECTION_KEYS) | {"ingredients", "instructions"}
    return {
        "stored_document_sha256": sha256_json(doc),
        "content_hash": doc.get("content_hash"),
        "evidence_fingerprint_current": evidence_fingerprint(candidate),
        "provenance": doc.get("provenance"),
        "current_admission": {
            "recommendable": candidate["recommendable"],
            "defects_blocking": candidate["defects_blocking"],
            "quality_context_codes": [c.get("code") for c in candidate["quality_context"]],
            "ingredient_list_vs_steps": "not_checked",
        },
        "sections": sections,
        "omitted_keys": sorted(k for k in doc if k not in shown),
        "omission_note": (
            "Omitted keys (raw row, media, ratings, nutrition, metadata, "
            "provenance detail) are not needed for this review; they remain "
            "in the stored document covered by stored_document_sha256. "
            "Ingredient and instruction lists are complete and verbatim."
        ),
        "_candidate": candidate,
    }


# ----------------------------------------------------------------- build ---


def build_example(
    spec: dict[str, Any], docs: dict[tuple[str, str], dict[str, Any]]
) -> dict[str, Any]:
    from culinary_copilot.recommendations.evidence import render_recipe

    artifact_path = ROOT / spec["artifact"]
    record = load_json(artifact_path)
    response = record.get("response") or {}
    cases = load_json(ROOT / spec["case_file"])
    case = next(c for c in cases["cases"] if c["id"] == spec["case_id"])
    evidence = response.get("evidence") or {}
    checks: dict[str, Any] = {"artifact_case_id_matches": record.get("case_id") == spec["case_id"]}
    source: dict[str, Any]
    if spec.get("identity"):
        ds, sid = spec["identity"]["dataset_id"], spec["identity"]["source_id"]
        block = source_block(ds, sid, docs[(ds, sid)])
        candidate = block.pop("_candidate")
        recorded_fp = evidence.get("evidence_fingerprint")
        fidelity = record.get("fidelity") or {}
        checks.update(
            {
                "selection_matches_identity": response.get("selection", {}).get("dataset_id") == ds
                and response.get("selection", {}).get("source_id") == sid,
                "saved_render_equals_current_render": render_recipe(candidate)
                == response.get("recipe"),
                "runner_fidelity_fingerprint_reproduced": fidelity.get("fingerprint")
                == runner_fidelity_fingerprint(ds, sid, block["content_hash"]),
                "evidence_fingerprint_reproduced": (
                    recorded_fp == block["evidence_fingerprint_current"]
                    if recorded_fp
                    else "not recorded (pre-repair-pass-4 artifact)"
                ),
            }
        )
        block["evidence_fingerprint_recorded"] = recorded_fp or "not recorded"
        source = block
    else:
        source = {
            "status": "not_recorded",
            "note": (
                "The saved response does not identify the assessed source. It is "
                "not reconstructed from a new search, because a search today "
                "need not return the historical candidates."
            ),
        }
    return {
        "id": spec["id"],
        "title": spec["title"],
        "categories": spec["categories"],
        "evidence_kind": spec["evidence_kind"],
        "contract_version": spec["contract_version"],
        "run": {
            "artifact": spec["artifact"],
            "artifact_sha256": sha256_file(artifact_path),
            "case_id": spec["case_id"],
            "mode": record.get("mode"),
            "model": record.get("model"),
            "reasoning": record.get("reasoning"),
            "status_code": record.get("status_code"),
            "spent_usd": record.get("spent_usd"),
            "usage_note": record.get("usage_note"),
        },
        "request_context": {
            "case_file": spec["case_file"],
            "case_file_sha256": sha256_file(ROOT / spec["case_file"]),
            "clarification": case.get("clarification"),
            "answer_steps": case.get("answer_steps"),
            "recommendation_request": case.get("recommendation_request"),
            "saved_revisions": {
                k: response.get(k)
                for k in ("request_id", "group_id", "request_revision", "group_revision")
            },
            "not_recorded": spec["not_recorded"],
        },
        "identity": spec.get("identity"),
        "offered_candidates": evidence.get("offered_candidates", "not recorded"),
        "source": source,
        "saved_response": response,
        "checks": checks,
        "judgments": spec["judgments"],
        "defects": spec["defects"],
        "unknowns": spec["unknowns"],
        "expected_backend_behavior": spec["expected_backend_behavior"],
        "linked_fixtures": spec["linked_fixtures"],
        "review_status": "pending_owner_acceptance",
    }


def build_fixture(
    spec: dict[str, Any], docs: dict[tuple[str, str], dict[str, Any]]
) -> dict[str, Any]:
    identity = spec["identity"]
    doc = docs[(identity["dataset_id"], identity["source_id"])]
    evidence = []
    for ref in spec["evidence"]:
        value = resolve_excerpt(doc, ref)
        evidence.append(
            {
                **{k: v for k, v in ref.items() if k != "sha256"},
                "excerpt": value,
                "sha256": ref["sha256"],
                "sha256_matches": excerpt_sha256(value) == ref["sha256"],
            }
        )
    current = sha256_json(doc)
    return {
        **{k: v for k, v in spec.items() if k != "evidence"},
        "evidence": evidence,
        "source_fingerprint_current": {
            "stored_document_sha256": current,
            "matches_proposal": current == spec["source_fingerprint"]["stored_document_sha256"],
        },
    }


def build_all() -> tuple[dict[str, str], dict[str, Any], list[str]]:
    examples = load_json(EXAMPLES)
    proposals = load_json(PROPOSALS)
    errors = check_spec(examples, proposals)
    identities = {
        (e["identity"]["dataset_id"], e["identity"]["source_id"])
        for e in examples["examples"]
        if e.get("identity")
    } | {(p["identity"]["dataset_id"], p["identity"]["source_id"]) for p in proposals["proposals"]}
    docs = fetch_documents(identities)
    built = [build_example(e, docs) for e in examples["examples"]]
    fixtures = [build_fixture(p, docs) for p in proposals["proposals"]]
    for example in built:
        for name, value in example["checks"].items():
            if value is False:
                errors.append(f"{example['id']}: check failed: {name}")
    for fixture in fixtures:
        if not fixture["source_fingerprint_current"]["matches_proposal"]:
            errors.append(f"{fixture['id']}: stored source changed since the proposal")
        for ref in fixture["evidence"]:
            if not ref["sha256_matches"]:
                errors.append(f"{fixture['id']}: excerpt hash mismatch at {ref}")
    header = {
        "version": examples["version"],
        "attribution": ATTRIBUTION,
        "reviewer": examples["reviewer"],
        "review_status": examples["review_status"],
        "contract_versions": examples["contract_versions"],
        "spec_sha256": {
            "examples.json": sha256_file(EXAMPLES),
            "enrichment_proposals.json": sha256_file(PROPOSALS),
        },
    }
    packet = {**header, "examples": built}
    fixture_doc = {
        "version": proposals["version"],
        "attribution": ATTRIBUTION,
        "note": proposals["note"],
        "fixtures": fixtures,
    }
    outputs = {
        "packet.json": dumps(packet),
        "packet.md": render_packet_md(packet),
        "enrichment_fixtures.json": dumps(fixture_doc),
        "enrichment_fixtures.md": render_fixtures_md(fixture_doc),
    }
    manifest = {
        "version": examples["version"],
        "attribution": ATTRIBUTION,
        "review_status": examples["review_status"],
        "spec_sha256": header["spec_sha256"],
        "artifacts": {e["run"]["artifact"]: e["run"]["artifact_sha256"] for e in built},
        "sources": {
            f"{ds}/{sid}": {
                "stored_document_sha256": sha256_json(doc),
                "content_hash": doc.get("content_hash"),
            }
            for (ds, sid), doc in sorted(docs.items())
        },
        "outputs_sha256": {name: sha256_text(text) for name, text in outputs.items()},
        "outputs_in_git": False,
        "note": (
            "Outputs contain source text and saved model responses and stay "
            "out of Git; this manifest holds identities and hashes only."
        ),
    }
    return outputs, manifest, errors


# -------------------------------------------------------------- markdown ---


def _cell(value: Any) -> str:
    text = "—" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ")


def render_packet_md(packet: dict[str, Any]) -> str:
    lines = [
        "# Phase 3 source-level review packet",
        "",
        f"**{packet['attribution']}**",
        "",
        f"Reviewer: {packet['reviewer']['name']} ({packet['reviewer']['type']}), "
        f"{packet['reviewer']['date']}. Status: `{packet['review_status']}`.",
        "",
        "Built by `scripts/recommendations_live/review_packet.py` from saved artifacts "
        "and stored sources (read-only). Historical observations are what a saved run "
        "did; current-policy expectations describe the code at the close-out pass.",
        "",
        "Contract versions:",
        "",
    ]
    for name, text in packet["contract_versions"].items():
        lines.append(f"- `{name}`: {text}")
    lines += ["", "| Example | Case | Source | Execution | Fidelity | Usefulness | Constraints |"]
    lines.append("|---|---|---|---|---|---|---|")
    for ex in packet["examples"]:
        ident = ex["identity"]
        source = f"{ident['dataset_id']} / {ident['source_id']}" if ident else "not recorded"
        j = ex["judgments"]
        lines.append(
            f"| {ex['id']} {_cell(ex['title'])} | {ex['run']['case_id']} | {_cell(source)} | "
            f"{j['execution']['verdict']} | {j['fidelity']['verdict']} | "
            f"{j['usefulness']['verdict']} | {j['constraint_support']['verdict']} |"
        )
    for ex in packet["examples"]:
        lines += render_example_md(ex)
    return "\n".join(lines) + "\n"


def render_example_md(ex: dict[str, Any]) -> list[str]:
    ctx = ex["request_context"]
    run = ex["run"]
    resp = ex["saved_response"]
    lines = [
        "",
        f"## {ex['id']}: {ex['title']}",
        "",
        f"Categories: {', '.join(ex['categories'])}. Evidence: `{ex['evidence_kind']}`. "
        f"Contract: `{ex['contract_version']}`. Review status: `{ex['review_status']}`.",
        "",
        "### Request context",
        "",
        f"- Case `{run['case_id']}` from `{ctx['case_file']}`.",
        f"- Clarification: `{json.dumps(ctx['clarification'], ensure_ascii=False)}`",
        f"- Answer steps: `{json.dumps(ctx['answer_steps'])}`; "
        f"recommendation request: `{json.dumps(ctx['recommendation_request'])}`",
        f"- Saved ids/revisions: `{json.dumps(ctx['saved_revisions'])}`",
        "- Not recorded:",
    ]
    lines += [f"  - {item}" for item in ctx["not_recorded"]]
    lines += [
        "",
        "### Run",
        "",
        f"`{run['artifact']}` (sha256 `{run['artifact_sha256'][:16]}…`): mode {run['mode']}, "
        f"model {run['model']}, reasoning {json.dumps(run['reasoning'])}, "
        f"HTTP {run['status_code']}, spend ${round(run['spent_usd'] or 0, 8)} "
        f"({run['usage_note']}).",
        "",
    ]
    offered = ex["offered_candidates"]
    if isinstance(offered, list):
        lines.append("Offered candidates (recorded):")
        lines += [
            f"- label {c['candidate_label']}: {c['dataset_id']} / {c['source_id']} "
            f'"{c["title"]}" (evidence `{c["evidence_fingerprint"][:16]}…`)'
            for c in offered
        ]
    else:
        lines.append("Offered candidates: **not recorded** in this artifact.")
    lines += ["", "### Source", ""]
    source = ex["source"]
    if source.get("status") == "not_recorded":
        lines.append(f"**Not recorded.** {source['note']}")
    else:
        sections = source["sections"]
        lines += [
            f"- Identity: `{ex['identity']['dataset_id']}` / `{ex['identity']['source_id']}`",
            f"- Stored document sha256 `{source['stored_document_sha256']}`; "
            f"content_hash `{source['content_hash']}`",
            f"- Evidence fingerprint (current code) `{source['evidence_fingerprint_current']}`; "
            f"recorded: `{source['evidence_fingerprint_recorded']}`",
            f"- Provenance: `{json.dumps(source['provenance'], ensure_ascii=False)[:400]}`",
            f"- Current admission: `{json.dumps(source['current_admission'])}`",
            f"- Title: {sections.get('title')}; servings: {sections.get('servings')}; "
            f"durations: `{json.dumps(sections.get('durations_minutes'))}`",
            f"- Flags: `{json.dumps(sections.get('flags'))}`",
            f"- Quality issues: `{json.dumps(sections.get('quality_issues'), ensure_ascii=False)}`",
        ]
        for key in ("unresolved_fields", "notes_text", "durations_reported"):
            if key in sections:
                lines.append(f"- {key}: `{json.dumps(sections[key], ensure_ascii=False)}`")
        lines += [
            "",
            "| Ref | Original line | Stored name | Quantity text | Unit | Notes |",
            "|---|---|---|---|---|---|",
        ]
        for item in sections["ingredients"]:
            lines.append(
                f"| {item['ref']} | {_cell(item.get('original'))} | "
                f"{_cell(item.get('canonical'))} | {_cell(item.get('quantity_text'))} | "
                f"{_cell(item.get('unit'))} | {_cell(item.get('notes'))} |"
            )
        lines += ["", "Instructions (verbatim):", ""]
        lines += [f"- `{s['ref']}` {s['text']}" for s in sections["instructions"]]
        omitted = ", ".join(source["omitted_keys"])
        lines += ["", f"Omitted keys: {omitted}. {source['omission_note']}"]
    lines += ["", "### Saved response", ""]
    lines.append(f"- Outcome: `{resp.get('outcome')}`")
    if resp.get("insufficient_reason"):
        reason = resp["insufficient_reason"]
        lines.append(f"- Insufficient reason: `{reason}`: {resp.get('detail')}")
    if resp.get("selection"):
        lines.append(f"- Selection: `{json.dumps(resp['selection'], ensure_ascii=False)}`")
    for key in ("selection_reasons", "needs"):
        if key in resp:
            lines.append(f"- {key}: `{json.dumps(resp[key], ensure_ascii=False)}`")
    if "rejected_propositions" in resp:
        lines.append(f"- rejected_propositions: `{json.dumps(resp['rejected_propositions'])}`")
    for c in resp.get("constraints", []):
        lines.append(f"- constraint `{c['constraint']}` = `{c['verdict']}`: {c['detail']}")
    if "discovery" in resp:
        lines.append(f"- discovery pointers: {len(resp['discovery'])}")
    epicure = resp.get("epicure") or {}
    lines.append(f"- Epicure: `{epicure.get('outcome')}` ({epicure.get('note')})")
    lines.append("- Full saved response: `packet.json`.")
    lines += ["", "### Checks (builder)", ""]
    lines += [f"- {name}: `{value}`" for name, value in ex["checks"].items()]
    lines += ["", "### Judgments", ""]
    for key in JUDGMENT_KEYS:
        j = ex["judgments"][key]
        lines.append(f"- **{key}**: `{j['verdict']}`. {j['rationale']}")
    lines += ["", "Defects:", ""] + [f"- {d}" for d in ex["defects"]]
    lines += ["", "Unknowns:", ""] + [f"- {u}" for u in ex["unknowns"]]
    expected = ex["expected_backend_behavior"]
    lines += [
        "",
        f"Expected backend behavior. Historical (observed): {expected['historical_observed']} "
        f"Current policy: {expected['current_policy']}",
    ]
    if ex["linked_fixtures"]:
        lines.append(f"\nLinked enrichment proposals: {', '.join(ex['linked_fixtures'])}.")
    return lines


def render_fixtures_md(doc: dict[str, Any]) -> str:
    lines = [
        "# Phase 3 proposed enrichment fixtures",
        "",
        f"**{doc['attribution']}**",
        "",
        doc["note"],
    ]
    for fx in doc["fixtures"]:
        ident = fx["identity"]
        lines += [
            "",
            f"## {fx['id']}: {fx['title']}",
            "",
            f"- Kind: `{fx['kind']}`; status `{fx['status']}`; owner decision "
            f"`{fx['owner_decision']}`; packet example {fx['packet_example']}.",
            f"- Source: `{ident['dataset_id']}` / `{ident['source_id']}`; stored document "
            f"sha256 `{fx['source_fingerprint']['stored_document_sha256']}` (current matches: "
            f"`{fx['source_fingerprint_current']['matches_proposal']}`).",
            f"- Reviewer: {fx['reviewer']['name']} ({fx['reviewer']['type']}), "
            f"{fx['reviewer']['date']}.",
            f"- Proposed change: `{json.dumps(fx['proposed_change'], ensure_ascii=False)}`",
            "",
            "Abstentions:",
            "",
        ]
        lines += [f"- {a}" for a in fx["abstentions"]]
        lines += ["", "Evidence (exact stored values):", ""]
        for ref in fx["evidence"]:
            where = " ".join(str(ref[k]) for k in ("section", "ref", "key") if k in ref)
            lines.append(
                f"- {where}: `{json.dumps(ref['excerpt'], ensure_ascii=False)}` "
                f"(hash matches: `{ref['sha256_matches']}`)"
            )
        lines += ["", "Establishes:", ""] + [f"- {e}" for e in fx["establishes"]]
        lines += ["", "Does not establish:", ""] + [f"- {e}" for e in fx["does_not_establish"]]
        lines += ["", f"Scope: {fx['scope']}"]
        if fx.get("effect_if_accepted"):
            lines += ["", f"Effect if accepted: {fx['effect_if_accepted']}"]
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------- cli ---


def _git_state() -> dict[str, Any]:
    def run(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True, check=False
        ).stdout.strip()

    return {"head": run("rev-parse", "HEAD"), "dirty": bool(run("status", "--porcelain"))}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=("check-spec", "build", "validate"))
    args = parser.parse_args(argv)
    if args.command == "check-spec":
        errors = check_spec(load_json(EXAMPLES), load_json(PROPOSALS))
        for error in errors:
            print(f"ERROR {error}")
        print("spec ok" if not errors else f"{len(errors)} spec error(s)")
        return 1 if errors else 0
    outputs, manifest, errors = build_all()
    if args.command == "build":
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        for name, text in outputs.items():
            (OUT_DIR / name).write_text(text, encoding="utf-8")
        (OUT_DIR / "manifest.json").write_text(
            dumps({**manifest, "built_at_git": _git_state()}), encoding="utf-8"
        )
        print(f"wrote {', '.join(OUTPUT_NAMES)} and manifest.json to {OUT_DIR}")
    else:
        for name, text in outputs.items():
            path = OUT_DIR / name
            if not path.exists():
                errors.append(f"{name}: missing; run build")
            elif path.read_text(encoding="utf-8") != text:
                errors.append(f"{name}: differs from a rebuild of current inputs")
        manifest_path = OUT_DIR / "manifest.json"
        if manifest_path.exists():
            saved = load_json(manifest_path)
            saved.pop("built_at_git", None)
            if saved != manifest:
                errors.append("manifest.json: differs from a rebuild of current inputs")
        else:
            errors.append("manifest.json: missing; run build")
    for error in errors:
        print(f"ERROR {error}")
    print(f"{args.command}: {'ok' if not errors else f'{len(errors)} error(s)'}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
