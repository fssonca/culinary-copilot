"""Resumable OpenAI Batch ingestion CLI (hybrid workflow).

The LLM interprets ambiguous source text; deterministic scripts do parsing,
validation, merging, provenance, indexing and database work::

    prepare  (offline)  routing -> batch-input.jsonl + mapping + manifest
    submit   (network)  upload file, create batch (explicit, confirmed)
    status   (network)  retrieve batch status (no long blocking)
    collect  (network)  download results, match by custom_id
    finalize (offline)  validate, merge, write ready_to_load.jsonl
    retry    (offline)  bounded retry batch for eligible failures only
    load     (DB)       upsert ready records; never snapshot-replaces

Uses the official OpenAI Python SDK against the real Batch API
(/v1/responses + strict structured outputs on the configured model).
No synchronous-request loop is ever described as batch mode.

State lives in the run directory so any command can resume after
interruption. Artifact writes are atomic (tmp + rename). Nothing is
submitted, uploaded or applied without an explicit command.
"""

import argparse
import csv
import json
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from culinary_copilot.config import Settings
from culinary_copilot.recipes import routing as routing_mod
from culinary_copilot.recipes.adapters.foodie import (
    FOODIE_ADAPTER_VERSION,
    FOODIE_DATASET,
    FOODIE_FILE,
    FOODIE_REVISION,
    FOODIE_SHA256,
    HEADING_CUE_RE,
    normalize_foodie_text,
    parse_ingredient_line,
    split_sections,
)
from culinary_copilot.recipes.dataset_utils import sha256_file, systematic_sample
from culinary_copilot.recipes.llm_cache import cache_key, entry_is_fresh
from culinary_copilot.recipes.llm_cache import read as cache_read
from culinary_copilot.recipes.llm_cache import write as cache_write
from culinary_copilot.recipes.llm_contracts import (
    EXTRACTION_SCHEMA_NAME,
    MAX_SOURCE_CHARS,
    PROMPT_VERSION,
    SCHEMA_VERSION,
    SYSTEM_PROMPT,
    ExtractionResponse,
    build_user_content,
    custom_id_for,
    extraction_json_schema,
    numbered_source,
    prompt_hash,
)
from culinary_copilot.recipes.llm_validate import (
    current_logic_versions,
    current_version_map,
    merge_response,
    merged_ingredient_problems,
    validate_response,
)
from culinary_copilot.recipes.routing import ROUTING_VERSION

BATCH_ENDPOINT = "/v1/responses"
COMPLETION_WINDOW = "24h"
# Documented 50% Batch discount vs synchronous prices
# (https://developers.openai.com/api/docs/guides/batch, 2026-09-22).
BATCH_DISCOUNT = 0.5
TERMINAL_STATUSES = {"completed", "failed", "expired", "cancelled"}

RECORD_STATES = (
    "parsed",
    "awaiting_llm",
    "llm_received",
    "validated",
    "unresolved",
    "quarantined",
    "ready_to_load",
    "loaded",
)


def atomic_write_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def estimate_tokens(text: str) -> int:
    """Rough input-token estimate (chars/4). Labeled estimate, never exact."""
    return max(1, len(text) // 4)


def build_request_brief(
    texts: str,
    result: dict[str, Any],
    recipe: dict[str, Any] | None,
    line_map: dict[str, str],
) -> dict[str, Any]:
    """Targeted brief: ambiguous lines with reference parses, settled fields,
    genuinely missing fields. Scripts own everything settled; the model
    resolves only what is flagged here."""
    from culinary_copilot.recipes.llm_contracts import build_brief
    from culinary_copilot.recipes.source_scope import ingredient_sources, requested_lines

    requested = requested_lines(recipe, line_map) if recipe is not None else list(line_map)
    if result.get("audit") and recipe is not None:
        requested = [
            item["source_line_id"]
            for item in ingredient_sources(recipe, line_map)
            if item.get("source_line_id")
        ]
    ambiguous = (
        [
            {
                "line_id": item["source_line_id"],
                "text": line_map[item["source_line_id"]],
                "reference_parse": {
                    "amount": item.get("amount"),
                    "unit": item.get("unit"),
                    "canonical": item.get("canonical"),
                },
            }
            for item in ingredient_sources(recipe, line_map)
            if item.get("source_line_id") in requested
        ]
        if recipe is not None
        else []
    )
    if recipe is None:
        settled: dict[str, Any] = {}
        missing = ["title", "ingredients", "headings", "steps", "servings", "notes"]
    else:
        settled = {
            "title": recipe["title"],
            "steps_count": len(recipe["instructions"]),
            "servings": recipe["servings"],
            "batch_yield": recipe["batch_yield"],
            "durations_count": len(recipe["durations_reported"]),
            "notes_count": len(recipe["notes_text"]) + len(recipe["attribution"]),
        }
        missing = []
        if not recipe["instructions"]:
            missing.append("steps")
        if (
            recipe["servings"] is None
            and recipe["batch_yield"] is None
            and routing_mod.servings_evidence(texts)
        ):
            missing.append("servings")
    brief = build_brief(
        routing_reasons=result["reason_codes"],
        ambiguous_lines=ambiguous,
        settled=settled,
        missing=missing,
    )
    if "durations_unstructured" in result.get("reason_codes", []):
        missing.append("durations")
    brief["mode"] = "audit" if result.get("audit") else "targeted"
    brief["requested_ingredient_line_ids"] = requested
    brief["single_line_abstention"] = recipe is None and len(line_map) == 1
    return brief


def estimate_cost_usd(input_tokens: int, output_tokens: int, settings: Settings) -> float | None:
    """Batch-discounted cost estimate, or None when pricing is unconfigured."""
    if settings.llm_price_input_per_1m is None or settings.llm_price_output_per_1m is None:
        return None
    sync = (
        input_tokens / 1e6 * settings.llm_price_input_per_1m
        + output_tokens / 1e6 * settings.llm_price_output_per_1m
    )
    return round(sync * BATCH_DISCOUNT, 4)


def block_line_ids(texts: str) -> tuple[list[str], list[str], list[str], dict[str, str]]:
    """Map ingredient/step/heading block lines to stable global L-ids (ordered match)."""
    _, line_map = numbered_source(texts)
    ordered = [lid for lid in line_map if line_map[lid]]
    try:
        sections = split_sections(texts)
    except ValueError:
        # Marker-less records routed for LLM segmentation have no
        # deterministic blocks; the model covers lines from scratch.
        return [], [], [], line_map
    from culinary_copilot.recipes.source_scope import comparison_text

    remaining = list(ordered)

    def align(wanted: list[str]) -> list[str]:
        found: list[str] = []
        for want in (w.strip() for w in wanted):
            for pos, lid in enumerate(remaining):
                if comparison_text(line_map[lid]) == comparison_text(want):
                    found.append(lid)
                    del remaining[pos]
                    break
        return found

    # Headings align first so the same text is never double-counted as both
    # a heading and an ingredient occurrence. Uses the identical rule as
    # normalize_foodie_text (trailing ":" or cue words on an unquantified line).
    heading_texts_wanted = []
    for line in sections["ingredient_lines"]:
        stripped = line.strip()
        item = parse_ingredient_line(line)
        if stripped.endswith(":") or (
            item["amount"] is None
            and not item["qualitative"]
            and len(stripped) < 60
            and re.search(r"\d", stripped) is None
            and bool(HEADING_CUE_RE.search(stripped))
        ):
            heading_texts_wanted.append(stripped)
    heading_ids = align(heading_texts_wanted)
    heading_texts = {line_map[lid] for lid in heading_ids}
    ing_lines = [line for line in sections["ingredient_lines"] if line.strip() not in heading_texts]
    return (
        align(ing_lines),
        align(sections["instruction_lines"]),
        heading_ids,
        line_map,
    )


def prose_line_ids(texts: str) -> tuple[list[str], dict[str, str]]:
    """Description/intro prose lines with stable L-ids (ordered match).

    These lines are context, not ingredients or steps; the model must still
    account for them (description, notes, or explicitly unclassified) rather
    than silently dropping paragraphs and tips.
    """
    _, line_map = numbered_source(texts)
    # Prose = non-empty lines that are neither the title line, section
    # markers, nor claimed ingredient/step/heading content.
    ing, steps, headings, _ = block_line_ids(texts)
    claimed = set(ing) | set(steps) | set(headings)
    ordered = [lid for lid in line_map if line_map[lid]]
    return [lid for lid in ordered[1:] if lid not in claimed and _is_prose(line_map[lid])], line_map


def _is_prose(line: str) -> bool:
    text = line.strip()
    if not text:
        return False
    if re.match(r"(?i)^\s*(ingredients|directions?|method|instructions|introduction)\s*$", text):
        return False
    return True


def get_client(settings: Settings):  # type: ignore[no-untyped-def]
    """Official OpenAI SDK client. Import is lazy so offline use stays key-free."""
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError(
            "The openai package is required for submit/status/collect. "
            "Install it, or use the offline commands (prepare/finalize/retry)."
        ) from exc
    key = settings.openai_api_key.get_secret_value()
    if not key:
        raise RuntimeError("OPENAI_API_KEY is not configured.")
    return OpenAI(api_key=key)


REASONING_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max"})


def build_request_body(
    *,
    model: str,
    system: str,
    user_content: str,
    max_output_tokens: int,
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    """Responses body. `reasoning.effort` is sent only when configured.

    Effort values are model-dependent; unknown values fail fast server-side
    with a clear 400 rather than silently changing behavior.
    """
    if reasoning_effort is not None and reasoning_effort not in REASONING_EFFORTS:
        raise ValueError(f"Unknown reasoning effort {reasoning_effort!r}")
    body: dict[str, Any] = {
        "model": model,
        "input": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": EXTRACTION_SCHEMA_NAME,
                "strict": True,
                "schema": extraction_json_schema(),
            }
        },
        "max_output_tokens": max_output_tokens,
    }
    if reasoning_effort is not None:
        body["reasoning"] = {"effort": reasoning_effort}
    return body


# ---------------------------------------------------------------- prepare ---


def cmd_prepare(args: argparse.Namespace, settings: Settings) -> dict[str, Any]:
    """Offline: route records, write batch-input.jsonl + mapping + manifest."""
    run_dir = Path(args.run_dir)
    csv_path = Path(args.csv)
    checksum = sha256_file(csv_path)
    if checksum != FOODIE_SHA256:
        raise ValueError(f"foodie checksum mismatch: {checksum}")
    with csv_path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != ["texts"]:
            raise ValueError(f"unexpected foodie columns: {reader.fieldnames}")
        rows = [{"texts": row["texts"] or ""} for row in reader]
    if getattr(args, "rows", None):
        try:
            selected = sorted({int(part) for part in args.rows.split(",") if part.strip()})
        except ValueError:
            raise ValueError("--rows must be comma-separated 1-indexed row numbers")
        if not selected or min(selected) < 1 or max(selected) > len(rows):
            raise ValueError("--rows out of range for this source file")
    else:
        selected = systematic_sample(len(rows), args.size, args.seed)
    routed = routing_mod.route_pilot(
        [{"texts": r["texts"]} for r in rows],
        selected,
        audit_rate=args.audit_rate,
        audit_seed=args.audit_seed,
        route_durations=getattr(args, "route_unstructured_durations", False),
    )
    needs_llm = [r for r in routed if r["route"] == "needs_llm"]
    if len(needs_llm) > args.limit:
        raise ValueError(
            f"{len(needs_llm)} LLM requests exceed --limit {args.limit}; "
            "raise the limit explicitly or narrow the sample."
        )
    model = args.model
    if model != settings.llm_extraction_model:
        raise ValueError(
            f"Requested model {model!r} differs from configured "
            f"{settings.llm_extraction_model!r}; refusing silent substitution."
        )
    effort = getattr(args, "reasoning_effort", None) or settings.llm_reasoning_effort
    if effort is not None and effort not in REASONING_EFFORTS:
        raise ValueError(f"Unknown reasoning effort {effort!r}")
    requests: list[dict[str, Any]] = []
    mapping: dict[str, Any] = {}
    total_est = 0
    for result in routed:
        if result["route"] != "needs_llm":
            continue
        texts = rows[result["row_number"] - 1]["texts"]
        line_ids, line_map = numbered_source(texts)
        ing_ids, step_ids, heading_ids, _ = block_line_ids(texts)
        prose_ids, _ = prose_line_ids(texts)
        try:
            recipe = normalize_foodie_text(texts, result["row_number"])
            normalized_hash: str | None = recipe["content_hash"]
        except ValueError:
            recipe = None
            normalized_hash = None
        segments = _segment(line_ids, texts)
        for segment_suffix, seg_ids in segments:
            custom_id = custom_id_for(
                result["source_id"], result["content_hash"], segment=segment_suffix
            )
            if custom_id in mapping:
                raise ValueError(f"duplicate custom_id {custom_id}")
            request_brief = build_request_brief(texts, result, recipe, line_map)
            user_content = build_user_content(
                source_id=result["source_id"],
                content_hash=result["content_hash"],
                line_ids=seg_ids,
                line_map=line_map,
                brief=request_brief,
                segment=segment_suffix,
            )
            body = build_request_body(
                model=model,
                system=SYSTEM_PROMPT,
                user_content=user_content,
                max_output_tokens=args.max_output_tokens,
                reasoning_effort=effort,
            )
            requests.append(
                {"custom_id": custom_id, "method": "POST", "url": BATCH_ENDPOINT, "body": body}
            )
            total_est += estimate_tokens(SYSTEM_PROMPT + user_content)
            mapping[custom_id] = {
                "source_id": result["source_id"],
                "row_number": result["row_number"],
                "texts": texts,
                "content_hash": result["content_hash"],
                "normalized_hash": normalized_hash,
                "segment": segment_suffix,
                "reason_codes": result["reason_codes"],
                "audit": result["audit"],
                "requested_ingredient_line_ids": request_brief["requested_ingredient_line_ids"]
                if recipe is not None
                else None,
                "ingredient_line_ids": ing_ids,
                "step_line_ids": step_ids,
                "heading_line_ids": heading_ids,
                "prose_line_ids": prose_ids,
                "line_map": line_map,
                "request_versions": {
                    **current_version_map(
                        adapter_version=FOODIE_ADAPTER_VERSION,
                        routing_version=ROUTING_VERSION,
                    ),
                    "model": model,
                    "reasoning_effort": effort,
                },
                "cache_key": cache_key(
                    dataset_id=FOODIE_DATASET,
                    revision=FOODIE_REVISION,
                    source_id=result["source_id"],
                    content_hash=result["content_hash"],
                    adapter_version=FOODIE_ADAPTER_VERSION,
                    routing_version=ROUTING_VERSION,
                    prompt_version=PROMPT_VERSION,
                    prompt_hash=prompt_hash(),
                    schema_version=SCHEMA_VERSION,
                    model=model,
                    segment=segment_suffix,
                    reasoning_effort=effort,
                ),
            }
    input_path = run_dir / "batch-input.jsonl"
    input_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(input_path.parent), suffix=".tmp")
    with os.fdopen(fd, "w") as stream:
        for request in requests:
            stream.write(json.dumps(request, ensure_ascii=False) + "\n")
    os.replace(tmp, input_path)
    atomic_write_json(run_dir / "mapping.json", mapping)
    atomic_write_json(
        run_dir / "routing.json",
        {"results": routed, "summary": routing_mod.routing_summary(routed)},
    )
    # Every selected record is tracked, not just LLM-routed ones, so
    # finalization can preserve deterministic successes with explicit states.
    sources = {
        result["source_id"]: {
            "row_number": result["row_number"],
            "texts": rows[result["row_number"] - 1]["texts"],
            "content_hash": result["content_hash"],
            "route": result["route"],
            "reason_codes": result["reason_codes"],
            "audit": result["audit"],
        }
        for result in routed
    }
    atomic_write_json(run_dir / "sources.json", sources)
    output_est = len(requests) * args.max_output_tokens
    cost = estimate_cost_usd(total_est, output_est, settings)
    manifest = {
        "stage": "prepared",
        "dataset_id": FOODIE_DATASET,
        "revision": FOODIE_REVISION,
        "file_path": FOODIE_FILE,
        "file_sha256": checksum,
        "model": model,
        "reasoning_effort": effort,
        "experiment": getattr(args, "experiment", None),
        "supersedes": getattr(args, "supersedes", None),
        "endpoint": BATCH_ENDPOINT,
        "prompt_version": PROMPT_VERSION,
        "prompt_hash": prompt_hash(),
        "schema_version": SCHEMA_VERSION,
        "adapter_version": FOODIE_ADAPTER_VERSION,
        "routing_version": ROUTING_VERSION,
        "requests": len(requests),
        "estimated_input_tokens": total_est,
        "max_output_tokens_per_request": args.max_output_tokens,
        "estimated_output_tokens_max": output_est,
        "estimated_cost_usd": cost,
        "pricing_note": (
            "Batch 50% of sync list prices (docs 2026-09-22); unknown"
            if cost is None
            else "estimate, not exact"
        ),
        "request_limit": args.limit,
        "selected_rows": selected,
        "route_unstructured_durations": getattr(args, "route_unstructured_durations", False),
        "budget_usd": settings.llm_budget_usd,
        "batch_id": None,
        "input_file_id": None,
        "status_history": [],
        "created_at": _now(),
    }
    atomic_write_json(run_dir / "manifest.json", manifest)
    summary = routing_mod.routing_summary(routed)
    print(f"dataset: {FOODIE_DATASET}@{FOODIE_REVISION[:12]}")
    print(f"routed: {summary['routes']} reasons: {summary['reasons']}")
    print(f"requests: {len(requests)} model: {model}")
    print(f"est. input tokens: ~{total_est}, est. cost: {cost if cost is not None else 'unknown'}")
    print(f"artifacts: {run_dir}/{{batch-input.jsonl,mapping.json,manifest.json,routing.json}}")
    return manifest


def _segment(line_ids: list[str], texts: str) -> list[tuple[str | None, list[str]]]:
    """Split oversized records into traceable segments; never silently truncate."""
    if len(texts) <= MAX_SOURCE_CHARS:
        return [(None, line_ids)]
    per: int = max(1, len(line_ids) // ((len(texts) // MAX_SOURCE_CHARS) + 1))
    chunks = [line_ids[i : i + per] for i in range(0, len(line_ids), per)]
    return [(f"seg{i + 1}of{len(chunks)}", chunk) for i, chunk in enumerate(chunks)]


# ----------------------------------------------------------------- submit ---


def cmd_submit(args: argparse.Namespace, settings: Settings) -> dict[str, Any]:
    """Upload the prepared file and create the batch (explicit, confirmed)."""
    run_dir = Path(args.run_dir)
    manifest: dict[str, Any] = json.loads((run_dir / "manifest.json").read_text())
    if manifest.get("requests") == 0:
        raise ValueError("No LLM requests: run finalize locally; nothing to upload or submit.")
    if not settings.llm_ingestion_enabled and not args.yes_llm:
        raise ValueError("LLM ingestion is disabled (LLM_INGESTION_ENABLED=false).")
    if manifest.get("batch_id"):
        raise ValueError(
            f"Manifest already references {manifest['batch_id']}; run status "
            "to reconcile it instead of resubmitting (ambiguous failures are "
            "never silently resubmitted)."
        )
    attempt = manifest.get("submission_attempt") or {}
    if attempt.get("state") == "unknown":
        raise ValueError(
            "A previous submission attempt has unknown outcome (batch creation "
            "may have succeeded server-side). Run `status --reconcile` before "
            "submitting again."
        )
    if manifest.get("estimated_cost_usd") is not None and settings.llm_budget_usd is not None:
        if manifest["estimated_cost_usd"] > settings.llm_budget_usd:
            raise ValueError("Estimated cost exceeds LLM_BUDGET_USD; refusing.")
    if manifest["model"] != settings.llm_extraction_model:
        raise ValueError("Configured model changed since prepare; re-prepare first.")
    cost = manifest["estimated_cost_usd"]
    print(f"dataset: {manifest['dataset_id']}@{manifest['revision'][:12]}")
    print(
        f"requests: {manifest['requests']} model: {manifest['model']} "
        f"reasoning_effort: {manifest.get('reasoning_effort')}"
    )
    print(
        f"est. tokens in/max-out: {manifest['estimated_input_tokens']}/"
        f"{manifest['estimated_output_tokens_max']} "
        f"est. cost: {cost if cost is not None else 'unknown'}"
    )
    out_cap = manifest["max_output_tokens_per_request"]
    print(f"caps: out-tokens {out_cap}, limit {manifest['request_limit']}")
    if not args.yes:
        raise ValueError("Refusing without --yes (explicit submission confirmation).")
    client = get_client(settings)
    input_path = run_dir / "batch-input.jsonl"
    if manifest.get("input_file_id") and args.resume:
        file_id = manifest["input_file_id"]
        print(f"resuming with previously uploaded {file_id}")
    else:
        with input_path.open("rb") as stream:
            uploaded = client.files.create(file=stream, purpose="batch")
        file_id = uploaded.id
        manifest["input_file_id"] = file_id
        atomic_write_json(run_dir / "manifest.json", manifest)
    # Persist the attempt BEFORE batch creation so a timeout/crash leaves a
    # reconcilable record instead of an invitation to duplicate the batch.
    manifest["submission_attempt"] = {
        "input_file_id": file_id,
        "attempted_at": _now(),
        "state": "unknown",
    }
    atomic_write_json(run_dir / "manifest.json", manifest)
    try:
        batch = client.batches.create(
            input_file_id=file_id,
            endpoint=BATCH_ENDPOINT,
            completion_window=COMPLETION_WINDOW,
            metadata={
                "description": (
                    f"culinary-copilot {manifest['dataset_id']} {manifest['requests']}req"
                )
            },
        )
    except Exception:
        print(
            "Batch creation raised; outcome unknown. Manifest keeps the "
            "submission attempt — run `status --reconcile` before retrying."
        )
        raise
    manifest["batch_id"] = batch.id
    manifest["stage"] = "submitted"
    manifest["submission_attempt"] = {
        "input_file_id": file_id,
        "attempted_at": manifest["submission_attempt"]["attempted_at"],
        "state": "confirmed",
        "batch_id": batch.id,
    }
    manifest["status_history"].append({"at": _now(), "status": batch.status})
    manifest["submitted_at"] = _now()
    atomic_write_json(run_dir / "manifest.json", manifest)
    print(f"submitted {batch.id} status={batch.status}")
    return manifest


# ----------------------------------------------------------------- status ---


def cmd_status(args: argparse.Namespace, settings: Settings) -> dict[str, Any]:
    """Retrieve batch status once (and optionally bounded-wait); never blocks for hours."""
    run_dir = Path(args.run_dir)
    manifest: dict[str, Any] = json.loads((run_dir / "manifest.json").read_text())
    client = get_client(settings)
    if not manifest.get("batch_id"):
        if getattr(args, "reconcile", False):
            return _reconcile_attempt(run_dir, manifest, client)
        raise ValueError("Nothing submitted yet (no batch_id in manifest).")
    deadline = time.time() + min(args.wait, 600) if args.wait else 0
    while True:
        batch = client.batches.retrieve(manifest["batch_id"])
        manifest["status_history"].append({"at": _now(), "status": batch.status})
        atomic_write_json(run_dir / "manifest.json", manifest)
        print(f"{batch.id}: {batch.status} {getattr(batch, 'request_counts', '')}")
        if batch.status in TERMINAL_STATUSES or time.time() >= deadline or not args.wait:
            manifest["stage"] = "submitted"
            atomic_write_json(run_dir / "manifest.json", manifest)
            return {"batch_id": batch.id, "status": batch.status}
        time.sleep(30)


# ---------------------------------------------------------------- collect ---


def _parse_response_body(
    body: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None, dict[str, Any] | None]:
    """Extract (parsed_json, refusal_note, usage) from a /v1/responses body."""
    usage: dict[str, Any] | None = None
    if isinstance(body.get("usage"), dict):
        usage = body["usage"]
    for item in body.get("output", []) or []:
        if item.get("type") == "refusal":
            return None, str(item.get("refusal", ""))[:300], usage
        if item.get("type") == "message":
            for content in item.get("content", []) or []:
                if content.get("type") == "refusal":
                    return None, str(content.get("refusal", ""))[:300], usage
                if content.get("type") == "output_text":
                    try:
                        return json.loads(content.get("text", "")), None, usage
                    except ValueError as exc:
                        return None, f"malformed_output: {exc}", usage
    status = body.get("status")
    if status == "incomplete":
        reason = body.get("incomplete_details", {}).get("reason", "unknown")
        return None, f"incomplete: {reason}", usage
    return None, "missing_output", usage


def cmd_collect(args: argparse.Namespace, settings: Settings) -> dict[str, Any]:
    """Download results/errors; match strictly by custom_id."""
    run_dir = Path(args.run_dir)
    manifest: dict[str, Any] = json.loads((run_dir / "manifest.json").read_text())
    mapping: dict[str, Any] = json.loads((run_dir / "mapping.json").read_text())
    if not manifest.get("batch_id"):
        raise ValueError("Nothing submitted yet.")
    client = get_client(settings)
    batch = client.batches.retrieve(manifest["batch_id"])
    if batch.status not in ("completed", "expired") and not args.force:
        raise ValueError(f"Batch is {batch.status}; collect after completion (or --force).")
    output_text = client.files.content(batch.output_file_id).text if batch.output_file_id else ""
    error_text = client.files.content(batch.error_file_id).text if batch.error_file_id else ""
    seen: dict[str, int] = {}
    collected: list[dict[str, Any]] = []
    collections_errors: list[dict[str, Any]] = []
    actual_in = actual_out = 0
    for line in _jsonl_lines(output_text):
        if not line.strip():
            continue
        record = json.loads(line)
        custom_id = record.get("custom_id")
        seen[custom_id] = seen.get(custom_id, 0) + 1
        if custom_id not in mapping:
            collections_errors.append({"custom_id": custom_id, "code": "unknown_result_id"})
            continue
        if seen[custom_id] > 1:
            collections_errors.append({"custom_id": custom_id, "code": "duplicate_result_id"})
            continue
        entry = mapping[custom_id]
        response = record.get("response") or {}
        if response.get("status_code") != 200:
            body = response.get("body") or {}
            api_error = body.get("error") or {}
            collected.append(
                {
                    "custom_id": custom_id,
                    **_ctx(entry),
                    "ok": False,
                    "code": api_error.get("code") or "request_error",
                    "detail": str(api_error.get("message", ""))[:300] or str(response)[:300],
                    "usage": None,
                }
            )
            continue
        parsed_json, failure, usage = _parse_response_body(response.get("body", {}))
        if usage:
            actual_in += int(usage.get("input_tokens", 0) or 0)
            actual_out += int(usage.get("output_tokens", 0) or 0)
        collected.append(
            {
                "custom_id": custom_id,
                **_ctx(entry),
                "ok": parsed_json is not None,
                "code": None if parsed_json is not None else "model_failure",
                "detail": failure,
                "parsed": parsed_json,
                "usage": usage,
                "api_request_id": response.get("request_id"),
                "model": (response.get("body", {}) or {}).get("model"),
            }
        )
    for line in _jsonl_lines(error_text):
        if not line.strip():
            continue
        record = json.loads(line)
        custom_id = record.get("custom_id")
        seen[custom_id] = seen.get(custom_id, 0) + 1
        if custom_id not in mapping:
            collections_errors.append({"custom_id": custom_id, "code": "unknown_error_id"})
            continue
        if seen[custom_id] > 1:
            collections_errors.append({"custom_id": custom_id, "code": "duplicate_result_id"})
            continue
        # Error-file lines may carry the failed `response` inline (e.g. a
        # 400 with invalid_json_schema); surface its message, not an empty
        # error object.
        response = record.get("response") or {}
        body = response.get("body") or {}
        api_error = body.get("error") or {}
        detail = (
            str(api_error.get("message", ""))[:300]
            or str((record.get("error") or {}).get("message", ""))[:300]
            or f"status={response.get('status_code')}"
        )
        code = (
            api_error.get("code")
            or (record.get("error") or {}).get("code")
            or ("request_error" if response else "batch_error")
        )
        collected.append(
            {
                "custom_id": custom_id,
                **_ctx(mapping[custom_id]),
                "ok": False,
                "code": code,
                "detail": detail,
                "usage": None,
            }
        )
    missing = [cid for cid in mapping if cid not in seen]
    for custom_id in missing:
        collected.append(
            {
                "custom_id": custom_id,
                **_ctx(mapping[custom_id]),
                "ok": False,
                "code": "missing_result",
                "detail": "no result line for request",
                "usage": None,
            }
        )
    _write_jsonl(run_dir / "results.jsonl", [c for c in collected if c["ok"]])
    _write_jsonl(run_dir / "result-errors.jsonl", [c for c in collected if not c["ok"]])
    actual_cost = estimate_cost_usd(actual_in, actual_out, settings)
    report = {
        "batch_id": manifest["batch_id"],
        "status": batch.status,
        "requested": len(mapping),
        "ok": sum(1 for c in collected if c["ok"]),
        "failed": sum(1 for c in collected if not c["ok"]),
        "unknown_or_duplicate_ids": collections_errors,
        "missing_ids": missing,
        "actual_input_tokens": actual_in,
        "actual_output_tokens": actual_out,
        "estimated_actual_cost_usd": actual_cost,
        "includes_retries": False,
        "collected_at": _now(),
    }
    atomic_write_json(run_dir / "collect-report.json", report)
    manifest["stage"] = "collected"
    manifest["status_history"].append({"at": _now(), "status": f"collected:{batch.status}"})
    atomic_write_json(run_dir / "manifest.json", manifest)
    print(json.dumps(report, indent=2))
    return report


def _is_loadable(merged: dict[str, Any]) -> bool:
    """A merged recipe can only load with a title and some content.

    Accepted/partial records without a title or without ingredients and
    instructions would violate the recipes NOT NULL constraint at load and
    abort the whole transaction. They stay terminally unresolved with an
    explicit detail instead of silently blocking 100+ good rows.
    """
    return (
        bool(merged.get("title"))
        and bool(merged.get("ingredients") or merged.get("instructions"))
        and not merged_ingredient_problems(merged)
    )


def _ctx(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_id": entry["source_id"],
        "row_number": entry["row_number"],
        "content_hash": entry["content_hash"],
        "segment": entry["segment"],
        "audit": entry["audit"],
    }


# --------------------------------------------------------------- finalize ---


def cmd_finalize(args: argparse.Namespace, settings: Settings) -> dict[str, Any]:
    """Offline: validate interpretations, merge, write ingestion-ready artifacts."""
    run_dir = Path(args.run_dir)
    cache_dir = Path(args.cache_dir)
    manifest: dict[str, Any] = json.loads((run_dir / "manifest.json").read_text())
    mapping: dict[str, Any] = json.loads((run_dir / "mapping.json").read_text())
    sources: dict[str, Any] = json.loads((run_dir / "sources.json").read_text())
    # Deterministic re-parsing must reproduce prepare-time output exactly.
    _require_versions(manifest)
    collected = _read_jsonl(run_dir / "results.jsonl")
    failed = _read_jsonl(run_dir / "result-errors.jsonl")
    current = {
        **current_version_map(
            adapter_version=FOODIE_ADAPTER_VERSION, routing_version=ROUTING_VERSION
        ),
        "model": manifest["model"],
        # Generation settings travel with the run: a request prepared under
        # a different effort than the run declares is stale by construction.
        "reasoning_effort": manifest.get("reasoning_effort"),
    }
    records: dict[str, dict[str, Any]] = {}
    ready: list[dict[str, Any]] = []
    validations: list[dict[str, Any]] = []
    quarantined: list[dict[str, Any]] = []
    # Deterministic successes and quarantines are preserved with explicit
    # final states — finalization never drops selected records.
    for source_id, source in sources.items():
        if source["route"] == "deterministic_accept":
            recipe = normalize_foodie_text(source["texts"], source["row_number"])
            recipe["provenance"] = {**recipe.get("provenance", {}), "llm": None}
            loadable = _is_loadable(recipe)
            if loadable:
                ready.append(recipe)
            records[source_id] = {
                "source_id": source_id,
                "row_number": source["row_number"],
                "state": "ready_to_load" if loadable else "unresolved",
                "verdict": "deterministic_accept" if loadable else "merged_evidence_failed",
                "attempts": 0,
            }
        elif source["route"] == "quarantine":
            quarantined.append(
                {
                    "source_id": source_id,
                    "row_number": source["row_number"],
                    "reason": (source["reason_codes"] or ["quarantine"])[0],
                    "raw": {"texts": source["texts"]},
                }
            )
            records[source_id] = {
                "source_id": source_id,
                "row_number": source["row_number"],
                "state": "quarantined",
                "verdict": (source["reason_codes"] or ["quarantine"])[0],
                "attempts": 0,
            }
    by_source: dict[str, list[dict[str, Any]]] = {}
    for entry in collected:
        by_source.setdefault(entry["source_id"], []).append(entry)
    for source_id, entries in by_source.items():
        first = mapping[entries[0]["custom_id"]]
        latched = _load_latched(run_dir, source_id)
        merged_state: dict[str, Any] = {
            "source_id": source_id,
            "row_number": first["row_number"],
            "state": "llm_received",
            "attempts": latched.get("attempts", 1),
        }
        if len(entries) > 1 or first["segment"] is not None:
            merged_state["state"] = "unresolved"
            merged_state["detail"] = "segmented record: segment merging deferred, raw retained"
            records[source_id] = merged_state
            continue
        entry = entries[0]
        cached = cache_read(cache_dir, first["cache_key"])
        # A cache hit is usable only with an accepted verdict AND current
        # validation/merge logic versions. Legacy entries (no versions) or
        # entries from older logic fall through to full revalidation and
        # remerging of the saved raw response — stale merges can never
        # bypass corrected checks, and a miss never triggers a paid
        # request (finalize is offline; raw responses are already saved).
        if (
            cached is not None
            and cached.get("validation", {}).get("verdict")
            in (
                "accepted",
                "accepted_partial",
            )
            and entry_is_fresh(cached)
        ):
            merged_state.update(
                state="validated", verdict=cached["validation"]["verdict"], from_cache=True
            )
            records[source_id] = merged_state
            if merged_state["verdict"] in ("accepted", "accepted_partial"):
                if _is_loadable(cached["merged"]):
                    ready.append(cached["merged"])
                    merged_state["state"] = "ready_to_load"
                else:
                    merged_state.update(
                        state="unresolved",
                        detail="evidence_unusable: title/content or ingredient evidence failed",
                    )
            continue
        try:
            deterministic = normalize_foodie_text(first["texts"], first["row_number"])
        except ValueError:
            deterministic = None
        from culinary_copilot.recipes.source_scope import requested_lines

        baseline = {
            "requested_ingredient_line_ids": first.get("requested_ingredient_line_ids")
            if "requested_ingredient_line_ids" in first
            else (
                requested_lines(deterministic, first["line_map"])
                if deterministic is not None
                else None
            ),
            "has_deterministic": deterministic is not None,
            "servings": (deterministic or {}).get("servings"),
            "batch_yield_count": ((deterministic or {}).get("batch_yield") or {}).get("count"),
            "has_steps": bool((deterministic or {}).get("instructions")),
            "has_groups": bool((deterministic or {}).get("ingredient_groups")),
            "has_title": bool((deterministic or {}).get("title")),
        }
        validation = validate_response(
            entry["parsed"],
            source_id=source_id,
            content_hash=first["content_hash"],
            line_map=first["line_map"],
            ingredient_line_ids=first["ingredient_line_ids"],
            step_line_ids=first["step_line_ids"],
            heading_line_ids=first.get("heading_line_ids", []),
            prose_line_ids=first.get("prose_line_ids", []),
            baseline=baseline,
            request_versions={k: v for k, v in first["request_versions"].items() if k != "model"},
            current_versions={k: v for k, v in current.items() if k != "model"},
        )
        validations.append({"source_id": source_id, **validation})
        if validation["verdict"] == "not_a_recipe":
            merged_state.update(
                state="unresolved", verdict="not_a_recipe", detail="multi-recipe boundary preserved"
            )
        elif validation.get("load_eligible") is False:
            merged_state.update(
                state="unresolved",
                verdict=validation["verdict"],
                detail="single_line_requires_spans",
            )
        elif validation["verdict"] in ("accepted", "accepted_partial"):
            parsed_model = ExtractionResponse.model_validate(entry["parsed"])
            merged = merge_response(
                parsed_model,
                deterministic=deterministic,
                validation=validation,
                line_map=first["line_map"],
            )
            post_merge = merged_ingredient_problems(merged)
            if post_merge:
                validation["problems"].extend(post_merge)
            merged["provenance"] = {
                **(deterministic or {}).get("provenance", {}),
                "llm": {
                    "model_requested": manifest["model"],
                    "reasoning_effort": manifest.get("reasoning_effort"),
                    "model_returned": entry.get("model"),
                    "prompt_version": PROMPT_VERSION,
                    "schema_version": SCHEMA_VERSION,
                    "batch_id": manifest.get("batch_id"),
                    "custom_id": entry["custom_id"],
                    "api_request_id": entry.get("api_request_id"),
                    "usage": entry.get("usage"),
                    "validated_at": _now(),
                },
            }
            cache_write(
                cache_dir,
                first["cache_key"],
                {
                    "validation": validation,
                    "merged": merged,
                    "source_id": source_id,
                    **current_logic_versions(),
                },
            )
            if _is_loadable(merged):
                merged_state.update(state="ready_to_load", verdict=validation["verdict"])
                ready.append(merged)
            else:
                merged_state.update(
                    state="unresolved",
                    verdict=validation["verdict"],
                    detail="evidence_unusable: title/content or ingredient evidence failed",
                )
        elif validation.get("retry_eligible") and merged_state["attempts"] < (
            settings.llm_retry_limit
        ):
            merged_state.update(
                state="awaiting_llm", verdict="rejected_validation", detail="eligible for retry"
            )
        elif validation.get("retry_eligible"):
            # Retry budget exhausted: terminal, explicit, never silently reset.
            merged_state.update(
                state="unresolved",
                verdict="rejected_validation",
                detail=_exhaustion_reason("rejected_validation", ""),
            )
        else:
            merged_state.update(state="unresolved", verdict="rejected_validation")
        records[source_id] = merged_state
    for entry in failed:
        source_id = entry["source_id"]
        if source_id in records:
            continue
        latched = _load_latched(run_dir, source_id)
        attempts = latched.get("attempts", 1)
        retryable = entry["code"] in (
            "request_error",
            "model_failure",
            "batch_expired",
            "missing_result",
        )
        if retryable and attempts < settings.llm_retry_limit:
            state, detail = "awaiting_llm", entry.get("detail", "")[:200]
        elif retryable:
            state, detail = "unresolved", _exhaustion_reason(entry["code"], entry.get("detail", ""))
        else:
            state, detail = "unresolved", entry.get("detail", "")[:200]
        records[source_id] = {
            "source_id": source_id,
            "row_number": entry["row_number"],
            "state": state,
            "verdict": entry["code"],
            "detail": detail,
            "attempts": attempts,
        }
    _write_jsonl(run_dir / "ready_to_load.jsonl", ready)
    _write_jsonl(run_dir / "final-quarantine.jsonl", quarantined)
    atomic_write_json(run_dir / "validation-report.json", {"validations": validations})
    _write_jsonl(
        run_dir / "records.jsonl",
        [{**rec, "updated_at": _now()} for rec in records.values()],
    )
    manifest["stage"] = "finalized"
    atomic_write_json(run_dir / "manifest.json", manifest)
    summary = {
        "ready": len(ready),
        "from_cache": sum(1 for r in records.values() if r.get("from_cache")),
        "logic_versions": current_logic_versions(),
        "states": {
            s: sum(1 for r in records.values() if r["state"] == s)
            for s in RECORD_STATES
            if any(r["state"] == s for r in records.values())
        },
    }
    print(json.dumps(summary, indent=2))
    return summary


def _exhaustion_reason(code: str, detail: str) -> str:
    """Terminal reason when the retry budget is exhausted.

    `output_budget_exhausted` pins the specific, actionable cause (the model
    burned its output-token budget on reasoning); anything else is a generic
    `retry_budget_exhausted`. Raw inputs, responses, validations and attempt
    history are preserved alongside; the record stays out of ready_to_load.
    """
    if code == "model_failure" and "max_output_tokens" in (detail or ""):
        return "output_budget_exhausted"
    return "retry_budget_exhausted"


def _require_versions(manifest: dict[str, Any]) -> None:
    """Refuse to finalize against changed code: re-prepare instead of mixing."""
    current = current_version_map(
        adapter_version=FOODIE_ADAPTER_VERSION, routing_version=ROUTING_VERSION
    )
    for key in ("prompt_version", "schema_version", "adapter_version", "routing_version"):
        if manifest.get(key) != current[key]:
            raise ValueError(
                f"{key} changed since prepare ({manifest.get(key)} -> {current[key]}); "
                "re-prepare before finalizing."
            )


def _load_latched(run_dir: Path, source_id: str) -> dict[str, Any]:
    path = run_dir / "records.jsonl"
    if not path.exists():
        return {}
    for line in _jsonl_lines(path.read_text()):
        if not line.strip():
            continue
        record: dict[str, Any] = json.loads(line)
        if record.get("source_id") == source_id:
            return record
    return {}


# ------------------------------------------------------------------ retry ---


def cmd_retry(args: argparse.Namespace, settings: Settings) -> dict[str, Any]:
    """Offline: bounded retry batch for eligible failures only."""
    run_dir = Path(args.run_dir)
    manifest: dict[str, Any] = json.loads((run_dir / "manifest.json").read_text())
    if manifest.get("stage") != "finalized":
        raise ValueError("Finalize before retrying.")
    records = [
        json.loads(line)
        for line in _jsonl_lines((run_dir / "records.jsonl").read_text())
        if line.strip()
    ]
    # Eligible = awaiting_llm WITH budget remaining. Validated, unresolved
    # and quarantined records are never retried; records finalized under an
    # older rule that left them awaiting_llm at the attempt limit are also
    # excluded here (their terminal transition happens at next finalize).
    eligible = [
        r
        for r in records
        if r["state"] == "awaiting_llm"
        and int(r.get("attempts", 1) or 1) < settings.llm_retry_limit
    ]
    if len(eligible) > args.limit:
        raise ValueError(f"{len(eligible)} eligible exceed --limit {args.limit}.")
    mapping: dict[str, Any] = json.loads((run_dir / "mapping.json").read_text())
    sources: dict[str, Any] = json.loads((run_dir / "sources.json").read_text())
    retry_requests = []
    retry_mapping: dict[str, Any] = {}
    retry_sources: dict[str, Any] = {}
    retry_dir = run_dir / f"retry-{_now().replace(':', '')}"
    cap_override = getattr(args, "max_output_tokens", None)
    for record in eligible:
        original = next(cid for cid, m in mapping.items() if m["source_id"] == record["source_id"])
        body_line = _find_request(run_dir, original)
        if cap_override is not None:
            body_line = {
                **body_line,
                "body": {**body_line["body"], "max_output_tokens": cap_override},
            }
        new_id = f"{original}:retry{record.get('attempts', 1) + 1}"
        retry_requests.append({**body_line, "custom_id": new_id})
        prior = mapping[original]
        retry_mapping[new_id] = {**prior, "attempt": record.get("attempts", 1) + 1}
        retry_sources[record["source_id"]] = sources[record["source_id"]]
    # A retry directory is a full run directory: the normal submit/status/
    # collect/finalize commands work on it unchanged via --run-dir.
    _write_jsonl(retry_dir / "batch-input.jsonl", retry_requests)
    atomic_write_json(retry_dir / "mapping.json", retry_mapping)
    atomic_write_json(retry_dir / "sources.json", retry_sources)
    _write_jsonl(
        retry_dir / "records.jsonl",
        [
            {
                "source_id": r["source_id"],
                "row_number": r["row_number"],
                "state": "awaiting_llm",
                "attempts": r.get("attempts", 1) + 1,
                "updated_at": _now(),
            }
            for r in eligible
        ],
    )
    total_est = sum(
        estimate_tokens(SYSTEM_PROMPT + json.dumps(request["body"])) for request in retry_requests
    )
    max_out = len(retry_requests) * manifest["max_output_tokens_per_request"]
    atomic_write_json(
        retry_dir / "manifest.json",
        {
            "stage": "prepared",
            "kind": "retry",
            "parent_run": str(run_dir),
            "parent_batch": manifest.get("batch_id"),
            "dataset_id": manifest["dataset_id"],
            "revision": manifest["revision"],
            "file_path": manifest["file_path"],
            "file_sha256": manifest["file_sha256"],
            "model": manifest["model"],
            "reasoning_effort": manifest.get("reasoning_effort"),
            "endpoint": manifest["endpoint"],
            "prompt_version": manifest["prompt_version"],
            "prompt_hash": manifest["prompt_hash"],
            "schema_version": manifest["schema_version"],
            "adapter_version": manifest["adapter_version"],
            "routing_version": manifest["routing_version"],
            "requests": len(retry_requests),
            "estimated_input_tokens": total_est,
            "max_output_tokens_per_request": cap_override
            or manifest["max_output_tokens_per_request"],
            "estimated_output_tokens_max": max_out,
            "estimated_cost_usd": estimate_cost_usd(total_est, max_out, settings),
            "pricing_note": "Batch 50% of sync list prices; estimate, not exact",
            "request_limit": args.limit,
            "budget_usd": settings.llm_budget_usd,
            "batch_id": None,
            "input_file_id": None,
            "status_history": [],
            "created_at": _now(),
        },
    )
    print(f"retry batch prepared: {len(retry_requests)} requests in {retry_dir}")
    return {"retry_requests": len(retry_requests), "dir": str(retry_dir)}


def cmd_reconcile(args: argparse.Namespace, settings: Settings) -> dict[str, Any]:
    """Merge a finalized retry run back into its parent run.

    Child record states overwrite the parent's for retried sources; child
    ready rows append to the parent's ready_to_load (deduped by source_id);
    child validations append to the parent's validation report; token usage
    adds up and the cost estimate is recomputed. The child directory is left
    intact as the audit trail.
    """
    parent = Path(args.run_dir)
    child = Path(args.retry_dir)
    child_manifest: dict[str, Any] = json.loads((child / "manifest.json").read_text())
    if child_manifest.get("kind") != "retry" or child_manifest.get("parent_run") != str(parent):
        raise ValueError("Retry directory does not belong to this parent run.")
    if child_manifest.get("stage") != "finalized":
        raise ValueError("Finalize the retry run before reconciling it.")
    parent_validations_early = json.loads((parent / "validation-report.json").read_text())
    already = [
        entry
        for entry in parent_validations_early.get("reconciled_retries", [])
        if entry.get("retry_dir") == str(child)
    ]
    if already:
        print(f"{child} already reconciled into {parent}; skipping (idempotent).")
        return {"records": None, "added_ready": 0, "skipped": True}
    parent_records = {
        json.loads(line)["source_id"]: json.loads(line)
        for line in _jsonl_lines((parent / "records.jsonl").read_text())
        if line.strip()
    }
    for line in _jsonl_lines((child / "records.jsonl").read_text()):
        if not line.strip():
            continue
        record = json.loads(line)
        prior = parent_records.get(record["source_id"], {})
        # Attempts never move backwards through reconciliation: a retry run
        # that somehow reports fewer attempts must not reset the budget.
        attempts = max(int(prior.get("attempts", 0) or 0), int(record.get("attempts", 0) or 0))
        parent_records[record["source_id"]] = {
            **record,
            "attempts": attempts,
            "retried_from": prior.get("state"),
            "updated_at": _now(),
        }
    _write_jsonl(parent / "records.jsonl", list(parent_records.values()))
    parent_ready = {
        json.loads(line)["source_id"]: line
        for line in _jsonl_lines((parent / "ready_to_load.jsonl").read_text())
        if line.strip()
    }
    added = 0
    for line in _jsonl_lines((child / "ready_to_load.jsonl").read_text()):
        if not line.strip():
            continue
        source_id = json.loads(line)["source_id"]
        if source_id not in parent_ready:
            added += 1
        parent_ready[source_id] = line
    (parent / "ready_to_load.jsonl").write_text("\n".join(parent_ready.values()) + "\n")
    parent_validations = json.loads((parent / "validation-report.json").read_text())
    child_validations = json.loads((child / "validation-report.json").read_text())
    parent_validations["validations"].extend(child_validations["validations"])
    parent_validations.setdefault("reconciled_retries", []).append(
        {"retry_dir": str(child), "at": _now()}
    )
    atomic_write_json(parent / "validation-report.json", parent_validations)
    parent_collect = json.loads((parent / "collect-report.json").read_text())
    child_collect = json.loads((child / "collect-report.json").read_text())
    parent_collect["actual_input_tokens"] += child_collect.get("actual_input_tokens", 0)
    parent_collect["actual_output_tokens"] += child_collect.get("actual_output_tokens", 0)
    parent_collect["estimated_actual_cost_usd"] = estimate_cost_usd(
        parent_collect["actual_input_tokens"], parent_collect["actual_output_tokens"], settings
    )
    parent_collect["includes_retries"] = True
    atomic_write_json(parent / "collect-report.json", parent_collect)
    print(f"reconciled {len(parent_records)} records, +{added} ready rows from {child}")
    return {"records": len(parent_records), "added_ready": added}


def _find_request(run_dir: Path, custom_id: str) -> dict[str, Any]:
    for line in _jsonl_lines((run_dir / "batch-input.jsonl").read_text()):
        if not line.strip():
            continue
        record: dict[str, Any] = json.loads(line)
        if record["custom_id"] == custom_id:
            return record
    raise ValueError(f"request {custom_id} not found")


# ------------------------------------------------------------------- load ---


def cmd_load(args: argparse.Namespace, settings: Settings) -> dict[str, Any]:
    """Load boundary: upsert ready records AND fully-rejected rows.

    Fully-rejected rows (routing quarantines + terminal validator
    rejections) persist in recipe_quarantine with a status, reason, verdict
    and the full problem list — never snapshot-replace a dataset.
    """
    from sqlalchemy import create_engine, text

    run_dir = Path(args.run_dir)
    records = [
        json.loads(line)
        for line in _jsonl_lines((run_dir / "records.jsonl").read_text())
        if line.strip()
    ]
    pending = [r for r in records if r["state"] in ("awaiting_llm", "llm_received")]
    if pending and not args.partial:
        raise ValueError(
            f"{len(pending)} records still pending; refusing full load. "
            "Re-run with --partial to load only ready records (non-destructive), "
            "or finish the batch first."
        )
    ready = [
        json.loads(line)
        for line in _jsonl_lines((run_dir / "ready_to_load.jsonl").read_text())
        if line.strip()
    ]
    invalid = [r["source_id"] for r in ready if not _is_loadable(r)]
    if invalid:
        raise ValueError(f"Ready records failed final evidence checks: {invalid[:10]}")
    manifest: dict[str, Any] = json.loads((run_dir / "manifest.json").read_text())
    engine = create_engine(settings.database_url.get_secret_value())
    try:
        from culinary_copilot.recipes.search import render_from_recipe

        with engine.begin() as conn:
            if getattr(args, "apply_schema", False):
                from culinary_copilot.recipes.import_data import apply_migrations

                applied = apply_migrations(conn)
                print(f"migrations applied: {applied or 'none pending'}")
            conn.execute(
                text("""
                INSERT INTO recipe_imports (id, dataset_id, revision, checksum,
                    normalizer_version, vocabulary_checksum, dataset_url, report)
                VALUES (:id, :dataset_id, :revision, :checksum,
                    :normalizer_version, :vocabulary_checksum, :dataset_url,
                    CAST(:report AS jsonb))
                ON CONFLICT (id) DO NOTHING
                """),
                {
                    "id": args.import_id,
                    "dataset_id": FOODIE_DATASET,
                    "revision": manifest.get("revision", FOODIE_REVISION),
                    "checksum": manifest.get("file_sha256", ""),
                    "normalizer_version": "3",
                    "vocabulary_checksum": "foodie-hybrid",
                    "dataset_url": f"https://huggingface.co/datasets/{FOODIE_DATASET}",
                    "report": json.dumps({"hybrid_run": str(run_dir)}),
                },
            )
            for recipe in ready:
                document = dict(recipe)
                conn.execute(
                    text("""
                    INSERT INTO recipes (dataset_id, source_id, import_id, title,
                        total_minutes, servings, ingredient_names, document, search_text)
                    VALUES (:dataset_id, :source_id, :import_id, :title, :minutes,
                        :servings, :names, CAST(:document AS jsonb), :search_text)
                    ON CONFLICT (dataset_id, source_id) DO UPDATE SET
                        title=EXCLUDED.title, total_minutes=EXCLUDED.total_minutes,
                        servings=EXCLUDED.servings, ingredient_names=EXCLUDED.ingredient_names,
                        document=EXCLUDED.document, search_text=EXCLUDED.search_text
                    """),
                    {
                        "dataset_id": recipe.get("dataset_id", FOODIE_DATASET),
                        "source_id": recipe["source_id"],
                        "import_id": args.import_id,
                        "title": recipe["title"],
                        "minutes": (recipe.get("durations_minutes") or {}).get("TotalTime"),
                        "servings": recipe.get("servings"),
                        "names": [
                            i.get("canonical") or i.get("name") for i in recipe["ingredients"]
                        ],
                        "document": json.dumps(document),
                        "search_text": render_from_recipe(recipe),
                    },
                )
            quarantined = _read_jsonl(run_dir / "final-quarantine.jsonl")
            validations = {}
            report_path = run_dir / "validation-report.json"
            if report_path.exists():
                validations = {
                    v["source_id"]: v
                    for v in json.loads(report_path.read_text()).get("validations", [])
                    if isinstance(v, dict) and "source_id" in v
                }
            sources = {}
            sources_path = run_dir / "sources.json"
            if sources_path.exists():
                sources = json.loads(sources_path.read_text())
            rejected: list[dict[str, Any]] = []
            for entry in quarantined:
                rejected.append(
                    {
                        "source_id": entry["source_id"],
                        "row_number": entry["row_number"],
                        "status": "quarantined",
                        "reason": entry.get("reason", "quarantine"),
                        "verdict": "quarantined",
                        "problems": [],
                        "raw": entry.get("raw", {}),
                    }
                )
            for record in records:
                if record.get("state") != "unresolved":
                    continue
                validation = validations.get(record["source_id"], {})
                problems = validation.get("problems", [])
                critical = [p for p in problems if p.get("class") == "critical"]
                reason = record.get("detail") or (
                    f"{critical[0]['code']}: {critical[0]['detail']}" if critical else "rejected"
                )
                source = sources.get(record["source_id"], {})
                rejected.append(
                    {
                        "source_id": record["source_id"],
                        "row_number": record["row_number"],
                        "status": "rejected",
                        "reason": str(reason)[:500],
                        "verdict": record.get("verdict", "rejected_validation"),
                        "problems": problems,
                        "raw": {"texts": source.get("texts", "")},
                    }
                )
            for entry in rejected:
                conn.execute(
                    text("""
                    INSERT INTO recipe_quarantine (import_id, row_number, source_id,
                        status, reason, verdict, problems, raw)
                    VALUES (:import_id, :row_number, :source_id,
                        :status, :reason, :verdict, CAST(:problems AS jsonb), CAST(:raw AS jsonb))
                    ON CONFLICT (import_id, row_number) DO UPDATE SET
                        source_id=EXCLUDED.source_id, status=EXCLUDED.status,
                        reason=EXCLUDED.reason, verdict=EXCLUDED.verdict,
                        problems=EXCLUDED.problems, raw=EXCLUDED.raw
                    """),
                    {
                        "import_id": args.import_id,
                        "row_number": entry["row_number"],
                        "source_id": entry["source_id"],
                        "status": entry["status"],
                        "reason": entry["reason"],
                        "verdict": entry["verdict"],
                        "problems": json.dumps(entry["problems"]),
                        "raw": json.dumps(entry["raw"]),
                    },
                )
    finally:
        engine.dispose()
    print(
        f"loaded {len(ready)} records + {len(rejected)} quarantined "
        f"(upsert, dataset-scoped, partial={args.partial})"
    )
    return {"loaded": len(ready), "quarantined": len(rejected), "partial": args.partial}


# ------------------------------------------------------------------ utils ---


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _jsonl_lines(text: str) -> list[str]:
    """Split JSONL text on newline characters only.

    str.splitlines() also splits on raw U+2028/U+2029, which survive
    json.dumps(ensure_ascii=False) inside string values and would corrupt
    multi-kilobyte merged rows (row 15599 class). The writer emits one
    JSON document per "\\n"-terminated line, so "\\n" is the only
    valid separator here.
    """
    return text.split("\n")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    with os.fdopen(fd, "w") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in _jsonl_lines(path.read_text()) if line.strip()]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", default="data/odunola-hybrid/run")
    parser.add_argument("--cache-dir", default="data/llm-cache")
    sub = parser.add_subparsers(dest="command", required=True)

    prep = sub.add_parser("prepare", help="offline routing + batch file")
    prep.add_argument("--csv", default=None)
    prep.add_argument("--size", type=int, default=150)
    prep.add_argument("--seed", type=int, default=42)
    prep.add_argument(
        "--rows",
        default=None,
        help="explicit 1-indexed source rows (e.g. 303,3423,18373) instead of sampling",
    )
    prep.add_argument("--model", default=None)
    prep.add_argument("--limit", type=int, default=None)
    prep.add_argument("--max-output-tokens", type=int, default=None)
    prep.add_argument("--audit-rate", type=float, default=None)
    prep.add_argument("--audit-seed", type=int, default=None)
    prep.add_argument("--reasoning-effort", default=None)
    prep.add_argument("--experiment", default=None)
    prep.add_argument("--supersedes", default=None)
    prep.add_argument(
        "--route-unstructured-durations",
        action="store_true",
        help="also route records whose timings are mentioned but unstructured",
    )

    submitted = sub.add_parser("submit", help="upload + create batch (explicit)")
    submitted.add_argument("--yes", action="store_true")
    submitted.add_argument("--yes-llm", action="store_true")
    submitted.add_argument("--resume", action="store_true")

    status = sub.add_parser("status", help="retrieve batch status once")
    status.add_argument("--wait", type=int, default=0)
    status.add_argument(
        "--reconcile",
        action="store_true",
        help="adopt a batch created by an ambiguous attempt (matches input_file_id)",
    )
    reconcile = sub.add_parser(
        "reconcile",
        help="merge a retry run's outcomes back into its parent run",
    )
    reconcile.add_argument("--retry-dir", required=True)

    collect = sub.add_parser("collect", help="download + correlate results")
    collect.add_argument("--force", action="store_true")

    sub.add_parser("finalize", help="offline validation + merge")
    retry = sub.add_parser("retry", help="bounded retry batch")
    retry.add_argument("--limit", type=int, default=None)
    retry.add_argument(
        "--max-output-tokens",
        type=int,
        default=None,
        help="override the per-request output cap (e.g. after max_output_tokens cutoffs)",
    )

    compare = sub.add_parser("compare", help="readable original-vs-extracted comparison (offline)")
    compare.add_argument("--output", default=None)

    load = sub.add_parser("load", help="upsert ready records (explicit)")
    load.add_argument("--import-id", required=True)
    load.add_argument("--partial", action="store_true")
    load.add_argument(
        "--apply-schema",
        action="store_true",
        help="apply pending versioned migrations first (same checksum-pinned "
        "procedure as the dataset importer; no import is triggered)",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    settings = Settings()
    if getattr(args, "model", None) is None and args.command == "prepare":
        args.model = settings.llm_extraction_model
    if getattr(args, "limit", None) is None:
        args.limit = settings.llm_batch_request_limit
    if getattr(args, "max_output_tokens", None) is None:
        args.max_output_tokens = settings.llm_max_output_tokens
    if getattr(args, "audit_rate", None) is None:
        args.audit_rate = settings.llm_audit_sample_rate
    if getattr(args, "audit_seed", None) is None:
        args.audit_seed = settings.llm_audit_seed
    if args.command == "prepare" and args.csv is None:
        from huggingface_hub import hf_hub_download

        args.csv = hf_hub_download(
            FOODIE_DATASET,
            FOODIE_FILE,
            repo_type="dataset",
            revision=FOODIE_REVISION,
            cache_dir=".cache/huggingface/hub",
        )
    commands = {
        "prepare": cmd_prepare,
        "submit": cmd_submit,
        "status": cmd_status,
        "collect": cmd_collect,
        "finalize": cmd_finalize,
        "retry": cmd_retry,
        "reconcile": cmd_reconcile,
        "compare": cmd_compare,
        "load": cmd_load,
    }
    commands[args.command](args, settings)


def _frac_or_none(value: object) -> str | None:
    """Normalized rational string for comparison (None stays None)."""
    if value is None:
        return None
    from fractions import Fraction

    try:
        return str(Fraction(str(value)))
    except (ValueError, ZeroDivisionError):
        return f"unparseable:{value}"


def cmd_compare(args: argparse.Namespace, settings: Settings) -> dict[str, Any]:
    """Offline readable comparison: source lines vs model extraction.

    For each collected model output, shows per-line coverage (missing vs
    interpreted vs explicitly unclassified), per-ingredient quantity/unit
    changes against the deterministic reference parse of the same source
    line, and instruction order/fidelity. Independent of the validation
    verdict — a rejected output is still compared so reviewers see what
    the model actually did.
    """
    del settings
    from culinary_copilot.recipes.adapters.foodie import parse_ingredient_line

    run_dir = Path(args.run_dir)
    mapping: dict[str, Any] = json.loads((run_dir / "mapping.json").read_text())
    collected = _read_jsonl(run_dir / "results.jsonl")
    validations = {
        v["source_id"]: v
        for v in json.loads((run_dir / "validation-report.json").read_text())["validations"]
    }
    sections_out: list[str] = []
    for entry in collected:
        ctx = mapping[entry["custom_id"]]
        parsed = entry["parsed"]
        line_map = ctx["line_map"]
        verdict = validations.get(entry["source_id"], {}).get("verdict", "unvalidated")
        lines = [f"## {entry['source_id']} — verdict {verdict}"]
        covered: set[str] = set()
        for item in parsed.get("ingredients", []):
            covered.add(item["source_line_id"])
            for lid in (item.get("evidence") or {}).get("line_ids", []):
                covered.add(lid)
        for step in parsed.get("steps", []):
            covered.add(step["source_line_id"])
        for note in parsed.get("notes", []):
            covered.add(note["source_line_id"])
        for head in parsed.get("headings", []):
            covered.add(head["source_line_id"])
        unclassified = set(parsed.get("unclassified_line_ids", []))
        import re as _re

        from culinary_copilot.recipes.llm_validate import canonical_unit

        title = parsed.get("title") or ""
        description = parsed.get("description") or ""
        lines.append("### Source lines vs extraction")
        for lid in sorted(line_map, key=lambda lid: int(lid[1:])):
            text = line_map[lid]
            if lid in covered:
                mark = "✓"
            elif (
                text == title
                or (description and text in description)
                or _re.match(r"(?i)^\s*(ingredients|directions?|method|introduction)\s*$", text)
            ):
                mark = "△ METADATA"
            elif lid in unclassified:
                mark = "○ UNCLASSIFIED"
            else:
                mark = "✗ MISSING"
            lines.append(f"- {mark} {lid}: {text[:110]}")
        lines.append("### Ingredient changes (model vs deterministic reference)")
        for item in parsed.get("ingredients", []):
            lid = item["source_line_id"]
            source_line = line_map.get(lid, "")
            try:
                ref = parse_ingredient_line(source_line)
                ref_desc = f"{ref['quantity_text']}/{ref['unit']}/{ref['canonical'][:40]}"
            except Exception:
                ref_desc = "reference parse failed"
            name = str(item.get("name") or "")[:40]
            got = f"{item.get('amount_text')}/{item.get('unit_normalized')}/{name}"
            flag = ""
            if ref_desc != "reference parse failed":
                if _frac_or_none(item.get("amount_value")) != _frac_or_none(ref["amount"]):
                    flag += " [AMOUNT CHANGED]"
                if canonical_unit(item.get("unit_normalized")) != (ref["unit"] or None):
                    flag += " [UNIT CHANGED]"
                if _re.search(r"\d", str(item.get("name") or "")):
                    flag += " [NAME HAS MEASURE]"
            lines.append(f"- {lid}: src={source_line[:80]!r} ref=({ref_desc}) model=({got}){flag}")
        lines.append("### Steps (order as emitted)")
        for pos, step in enumerate(parsed.get("steps", [])):
            lines.append(f"- {pos}: {step['source_line_id']} {step['text'][:110]!r}")
        sections_out.append("\n".join(lines))
    output = Path(getattr(args, "output", None) or run_dir / "comparison.md")
    output.write_text("\n\n".join(sections_out) + "\n")
    print(f"wrote {output} ({len(sections_out)} records)")
    return {"records": len(sections_out), "output": str(output)}


def _reconcile_attempt(run_dir: Path, manifest: dict[str, Any], client: Any) -> dict[str, Any]:
    """Adopt the batch of an ambiguous submission attempt, if it exists.

    Matches server-side batches by the persisted input_file_id. When no
    batch references our file, the attempt is marked absent and a fresh
    submit is allowed again. Batch listing can lag; the adopted outcome is
    recorded either way so the next submit never guesses.
    """
    attempt = manifest.get("submission_attempt") or {}
    file_id = attempt.get("input_file_id")
    if not file_id:
        raise ValueError("No submission attempt to reconcile.")
    found = None
    for batch in client.batches.list(limit=20).data:
        if getattr(batch, "input_file_id", None) == file_id:
            found = batch
            break
    if found is None:
        manifest["submission_attempt"] = {**attempt, "state": "absent", "reconciled_at": _now()}
        atomic_write_json(run_dir / "manifest.json", manifest)
        print(f"no batch references {file_id}; attempt marked absent, submit may proceed")
        return {"reconciled": "absent"}
    manifest["batch_id"] = found.id
    manifest["stage"] = "submitted"
    manifest["submission_attempt"] = {**attempt, "state": "confirmed", "batch_id": found.id}
    manifest["status_history"].append({"at": _now(), "status": found.status})
    atomic_write_json(run_dir / "manifest.json", manifest)
    print(f"adopted {found.id} status={found.status}")
    return {"reconciled": found.id, "status": found.status}


if __name__ == "__main__":
    main()
