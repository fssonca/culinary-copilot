"""Rolling-window Batch scheduler: at most three concurrent jobs.

Minimal coordination over the existing sequential runner
(:mod:`culinary_copilot.recipes.llm_batch`): this module adds slot
accounting, checkpoint-gated refills, the per-batch failure-rate rule,
global stop handling, an atomic budget ledger, and resumable persisted
state. It never touches extraction prompts, schemas, validators, merge
behavior, or model choice, and it never loads the application database.

Slot occupancy: a job occupies one of three slots while its server
status is validating/in_progress/finalizing/cancelling, while its
submission outcome is ambiguous (until reconciled), or while it is
terminal but not yet reviewed. A reviewed job frees its slot.
"""

import argparse
import json
import time
from argparse import Namespace
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from culinary_copilot.recipes.llm_batch import _jsonl_lines

MAX_IN_FLIGHT = 3
# Coordinator logic version. Bumped when scheduler rules change; the
# extraction snapshot (prompt/schema/adapter/routing/validator/merge) is
# tracked separately and must stay unchanged across scheduler fixes.
SCHEDULER_VERSION = "2"
TOTAL_CEILING_USD = 10.0
SLOT_STATUSES = {"validating", "in_progress", "finalizing", "cancelling"}
TERMINAL = {"completed", "failed", "expired", "cancelled"}


def rejection_breach(rejected: int, fresh_records: int) -> bool:
    """True when first-attempt rejections exceed 33% of fresh records.

    Exact integer rule: 100 * rejected > 33 * fresh_records. So 49/150
    passes while 50/150 stops; 66/200 passes while 67/200 stops.
    """

    return 100 * rejected > 33 * fresh_records


def first_attempt_rejections(run_dir: Path) -> tuple[int, int]:
    """(rejected, total) for a freshly finalized parent run.

    Rejection = verdict `rejected_validation` at attempt 1 (awaiting or
    terminally unresolved). Abstentions (`not_a_recipe`) and gate-diverted
    accepted/verdict rows are not rejections.
    """
    records = [
        json.loads(line)
        for line in _jsonl_lines((run_dir / "records.jsonl").read_text())
        if line.strip()
    ]
    first = [r for r in records if int(r.get("attempts", 1) or 1) == 1]
    rejected = sum(1 for r in first if r.get("verdict") == "rejected_validation")
    return rejected, len(first)


@contextmanager
def locked_state(scope_dir: Path) -> Generator[dict[str, Any], None, None]:
    """Single-coordinator atomic access: an exclusive file lock guards
    every read-modify-write cycle so concurrent workers cannot spend the
    same remaining budget."""
    import fcntl

    scope_dir.mkdir(parents=True, exist_ok=True)
    path = scope_dir / "sched-state.json"
    if not path.exists():
        path.write_text(json.dumps(_new_state(), indent=1))
    with path.open("r+") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            state = json.load(stream)
            yield state
            stream.seek(0)
            stream.truncate()
            json.dump(state, stream, indent=1)
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _new_state() -> dict[str, Any]:
    return {
        "ceiling": TOTAL_CEILING_USD,
        "versions": None,
        "jobs": {},
        "queue": [],
        "retry_parents": [],
        "stop": None,
        "completed_actual": 0.0,
        "reviews": {},
        "events": [],
    }


def current_versions() -> dict[str, str]:
    from culinary_copilot.recipes.adapters.foodie import FOODIE_ADAPTER_VERSION
    from culinary_copilot.recipes.llm_contracts import (
        PROMPT_VERSION,
        SCHEMA_VERSION,
        prompt_hash,
    )
    from culinary_copilot.recipes.llm_validate import MERGE_VERSION, VALIDATOR_VERSION
    from culinary_copilot.recipes.routing import ROUTING_VERSION

    return {
        "scheduler_version": SCHEDULER_VERSION,
        "prompt_version": PROMPT_VERSION,
        "prompt_hash": prompt_hash(),
        "schema_version": SCHEMA_VERSION,
        "adapter_version": FOODIE_ADAPTER_VERSION,
        "routing_version": ROUTING_VERSION,
        "validator_version": VALIDATOR_VERSION,
        "merge_version": MERGE_VERSION,
    }


def occupied_slots(state: dict[str, Any]) -> int:
    """Jobs holding a slot: submitted but unreviewed (any server status,
    including terminal-yet-unreviewed) plus ambiguous submissions."""
    return sum(
        1
        for job in state["jobs"].values()
        if (job.get("batch_id") or job.get("ambiguous")) and not job["stages"].get("reviewed")
    )


def reserved_total(state: dict[str, Any]) -> float:
    return float(
        round(
            sum(
                job["reserved_max"]
                for job in state["jobs"].values()
                if not job["stages"].get("reviewed")
            ),
            4,
        )
    )


def budget_ok(state: dict[str, Any], proposed_max: float) -> bool:
    left: float = round(float(state["completed_actual"]) + reserved_total(state) + proposed_max, 4)
    return left <= float(state["ceiling"])


def log(state: dict[str, Any], message: str) -> None:
    from culinary_copilot.recipes.llm_batch import _now

    state["events"].append({"at": _now(), "message": message})


def set_stop(state: dict[str, Any], reason: str, detail: str = "") -> None:
    if state["stop"] is None:
        from culinary_copilot.recipes.llm_batch import _now

        state["stop"] = {"reason": reason, "detail": detail, "at": _now()}
        log(state, f"STOP: {reason} {detail}")


def cap_based_max(run_dir: Path, settings: Any) -> float | None:
    """Conservative reservation from the prepare manifest (full output cap)."""
    from culinary_copilot.recipes.llm_batch import estimate_cost_usd

    manifest = json.loads((run_dir / "manifest.json").read_text())
    return estimate_cost_usd(
        manifest["estimated_input_tokens"],
        manifest["requests"] * manifest["max_output_tokens_per_request"],
        settings,
    )


class LiveBackend:
    """Production runner bindings (paid requests on submit)."""

    def __init__(self, settings: Any, cache_dir: Path | None = None):
        self.settings = settings
        self.cache_dir = cache_dir

    def _args(self, run_dir: Path | str, **over: Any) -> Namespace:
        run_dir = Path(run_dir)
        base = {
            "run_dir": str(run_dir),
            "cache_dir": str(self.cache_dir or (run_dir / "cache")),
            "yes": True,
            "yes_llm": True,
            "resume": False,
            "wait": 0,
            "force": False,
            "reconcile": False,
            "limit": 200,
            "max_output_tokens": None,
            "partial": False,
        }
        base.update(over)
        return Namespace(**base)

    def submit(self, run_dir: Path) -> str:
        from culinary_copilot.recipes import llm_batch

        batch_id: str = llm_batch.cmd_submit(self._args(run_dir), self.settings)["batch_id"]
        return batch_id

    def query(self, run_dir: Path) -> str:
        from culinary_copilot.recipes import llm_batch

        status: str = llm_batch.cmd_status(self._args(run_dir), self.settings)["status"]
        return status

    def reconcile_submission(self, run_dir: Path) -> str | None:
        from culinary_copilot.recipes import llm_batch

        try:
            out = llm_batch.cmd_status(self._args(run_dir, reconcile=True), self.settings)
        except Exception:
            return None
        return out.get("reconciled")

    def collect(self, run_dir: Path) -> dict[str, Any]:
        from culinary_copilot.recipes import llm_batch

        # Failed/cancelled batches may still hold partial results; collect
        # them (missing requests become explicit missing_result entries)
        # instead of dropping the job's evidence.
        status = self.query(run_dir)
        force = status in ("failed", "cancelled")
        return llm_batch.cmd_collect(self._args(run_dir, force=force), self.settings)

    def finalize(self, run_dir: Path) -> dict[str, Any]:
        from culinary_copilot.recipes import llm_batch

        return llm_batch.cmd_finalize(self._args(run_dir), self.settings)

    def prepare_retry(self, parent_dir: Path) -> Path:
        from culinary_copilot.recipes import llm_batch

        out = llm_batch.cmd_retry(self._args(parent_dir), self.settings)
        return Path(out["dir"])

    def reconcile_runs(self, child_dir: Path) -> dict[str, Any]:
        import json

        from culinary_copilot.recipes import llm_batch

        # Reconcile against the parent recorded in the child manifest
        # verbatim: the runner refuses cross-parent merges, and stored
        # paths may be relative while the scheduler tracks absolutes.
        parent_dir = json.loads((child_dir / "manifest.json").read_text())["parent_run"]
        return llm_batch.cmd_reconcile(
            self._args(parent_dir, retry_dir=str(child_dir)), self.settings
        )


def review_job(run_dir: Path, kind: str) -> dict[str, Any]:
    """Automated review gate for slot refill (not a correctness certificate).

    Verifies record accounting, capability integrity, evidence screens on
    every ready row, and — for fresh batches — the first-attempt
    failure-rate rule. Close-read sampling stays a manual tranche
    checkpoint before any application loading.
    """
    from culinary_copilot.recipes.llm_validate import _unit_evidenced

    records = [
        json.loads(line)
        for line in _jsonl_lines((run_dir / "records.jsonl").read_text())
        if line.strip()
    ]
    ready = [
        json.loads(line)
        for line in _jsonl_lines((run_dir / "ready_to_load.jsonl").read_text())
        if line.strip()
    ]
    validations = {
        v["source_id"]: v
        for v in json.loads((run_dir / "validation-report.json").read_text()).get("validations", [])
        if isinstance(v, dict) and "source_id" in v
    }
    findings: list[str] = []
    padding_candidates: list[str] = []
    coverage_notes: list[str] = []
    ready_ids = {r["source_id"] for r in ready}
    if any(r["source_id"] not in ready_ids and r["state"] == "ready_to_load" for r in records):
        findings.append("accounting: ready_to_load record without ready row")
    if any(r["source_id"] in ready_ids and r["state"] != "ready_to_load" for r in records):
        findings.append("accounting: ready row without ready_to_load record")
    by_source = {r["source_id"]: r for r in records if "source_id" in r}
    for row in ready:
        sid = row["source_id"]
        caps = row.get("capabilities", {}) or {}
        if row.get("scalable") or row.get("final_recipe_eligible"):
            if not any(f.startswith("capability:") for f in findings):
                findings.append(f"capability: {sid} overclaims scale/complete")
        if caps.get("quantities_validated") and row.get("llm_status") != "resolved":
            if not any(f.startswith("capability:") for f in findings):
                findings.append(f"capability: {sid} validated without resolve")
        prov = (row.get("provenance") or {}).get("llm")
        if not prov:
            if not any(f.startswith("provenance:") for f in findings):
                findings.append(f"provenance: {sid} missing llm block")
        ing = row.get("ingredients", [])
        steps = row.get("instructions", [])
        if not row.get("title") or not (ing or steps):
            # Mirrors the runner's loadability rule (title + content):
            # without these the row would abort the load transaction.
            if not any(f.startswith("coverage:") for f in findings):
                findings.append(
                    f"coverage: {sid} ready with title={bool(row.get('title'))} "
                    f"ingredients={len(ing)} steps={len(steps)}"
                )
        if not row.get("description"):
            # Description is not load-bearing (title + content loads);
            # whole source segments lack it. Surfaced, never a stop.
            coverage_notes.append(f"{sid}: ready without description")
        # Padding candidates for the manual close read (REVIEW-PLAN item
        # 1): same normalized name 3+ times. Deliberately NOT a stop —
        # honest multi-component recipes repeat genuine items across
        # distinct lines (000185 salt-and-pepper x4; 019453 six items on
        # one glued line), while true padding (16331-class junk) is a
        # semantic judgment no structural rule separates. Candidates are
        # surfaced; humans decide.
        names = [str(i.get("name") or "").strip().lower() for i in ing if i.get("name")]
        if names:
            from collections import Counter as _Counter

            for top, hits in _Counter(names).most_common():
                if hits >= 3:
                    padding_candidates.append(f"{row['source_id']}: {top!r} x{hits}")
                    break
        for item in ing:
            unit = item.get("unit")
            if unit in (None, "count", "container"):
                continue
            if not _unit_evidenced(unit, item.get("original", "")):
                if not any(f.startswith("unit-screen:") for f in findings):
                    findings.append(f"unit-screen: {sid}/{item.get('name')} unit {unit}")
                break
        for problem in validations.get(row["source_id"], {}).get("problems", []):
            if problem.get("code") in {
                "fabricated_evidence",
                "invented_servings",
                "invented_batch_yield",
                "invented_url",
            }:
                if not any(f.startswith("integrity:") for f in findings):
                    findings.append(f"integrity: {sid} ready with {problem['code']}")
                break
    rejected = total = 0
    rule = "100*rejected > 33*fresh_records"
    if kind == "fresh":
        rejected, total = first_attempt_rejections(run_dir)
        if rejection_breach(rejected, total):
            findings.append(
                f"failure-rate: {rejected}/{total} exceeds 33% (100*{rejected} > 33*{total})"
            )
    # Retry-converted clean accepts: every one is individually screened
    # above (screen-all); listed explicitly for the submission checkpoint.
    converted = sorted(
        sid
        for sid, rec in by_source.items()
        if sid in ready_ids
        and int(rec.get("attempts", 1) or 1) > 1
        and rec.get("state") == "ready_to_load"
    )
    ordered_ready = [r["source_id"] for r in ready]
    manual_review_records = ordered_ready[:30]
    for sid in converted:
        if sid not in manual_review_records:
            manual_review_records.append(sid)
    return {
        "kind": kind,
        "records": len(records),
        "ready": len(ready),
        "rejected_first": rejected,
        "total_first": total,
        "rule": rule,
        "findings": findings,
        "padding_candidates": padding_candidates,
        "coverage_notes": coverage_notes,
        "converted": converted,
        "manual_review_records": manual_review_records,
        "passed": not findings,
    }


def step(scope_dir: Path, backend: Any, settings: Any) -> dict[str, Any]:
    """One coordinator iteration: reconcile, collect, review, refill or stop."""
    actions: list[str] = []
    with locked_state(scope_dir) as state:
        current = current_versions()
        if state["versions"] is None:
            state["versions"] = current
            log(state, f"versions frozen: {state['versions']}")
        elif state["versions"] != current:
            frozen, now = state["versions"], current
            extraction_keys = [k for k in now if k != "scheduler_version"]
            if all(frozen.get(k) == now[k] for k in extraction_keys):
                # Scheduler-only change with extraction behavior frozen:
                # adopt the refreshed coordinator snapshot deliberately.
                state["versions"] = current
                log(state, f"scheduler snapshot refreshed: {frozen} -> {now}")
            else:
                set_stop(state, "version-change", f"{frozen} -> {now}")
                return {"actions": actions, "stop": state["stop"]}
        # 1. Reconcile in-flight jobs (status once each; never resubmit).
        for job_id, job in state["jobs"].items():
            if job["stages"].get("reviewed"):
                continue
            run_dir = Path(job["run_dir"])
            if job.get("ambiguous") and not job.get("batch_id"):
                batch_id = backend.reconcile_submission(run_dir)
                if batch_id:
                    job["batch_id"] = batch_id
                    job["ambiguous"] = False
                    actions.append(f"{job_id}: ambiguous submission reconciled to {batch_id}")
                    log(state, actions[-1])
                else:
                    actions.append(f"{job_id}: still ambiguous; slot held, no resubmit")
                continue
            if not job.get("batch_id"):
                continue
            try:
                status = backend.query(run_dir)
            except Exception as exc:
                actions.append(f"{job_id}: status query failed ({exc}); slot held")
                continue
            job["status"] = status
            if status in TERMINAL and not job["stages"].get("collected"):
                report = backend.collect(run_dir)
                job["stages"]["collected"] = True
                actual = report.get("estimated_actual_cost_usd")
                if actual is not None:
                    state["completed_actual"] = round(state["completed_actual"] + actual, 4)
                    job["actual"] = actual
                    job["reserved_max"] = 0.0
                actions.append(
                    f"{job_id}: collected {status} "
                    f"ok={report.get('ok')} failed={report.get('failed')}"
                )
                log(state, actions[-1])
        # 2. Finalize + review collected jobs (slot held until reviewed;
        #  a local failure holds the slot rather than killing the loop).
        for job_id, job in state["jobs"].items():
            if job["stages"].get("collected") and not job["stages"].get("finalized"):
                try:
                    backend.finalize(Path(job["run_dir"]))
                except Exception as exc:
                    actions.append(f"{job_id}: finalize failed ({exc}); slot held")
                    continue
                job["stages"]["finalized"] = True
                actions.append(f"{job_id}: finalized")
            if job["stages"].get("finalized") and not job["stages"].get("reviewed"):
                try:
                    review = review_job(Path(job["run_dir"]), job["kind"])
                except Exception as exc:
                    actions.append(f"{job_id}: review failed ({exc}); slot held")
                    continue
                state["reviews"][job_id] = review
                job["stages"]["reviewed"] = True
                actions.append(
                    f"{job_id}: reviewed passed={review['passed']} ready={review['ready']}"
                )
                log(state, actions[-1])
                residual = [
                    f
                    for f in review["findings"]
                    if not f.startswith(("failure-rate:", "integrity:", "unit-screen:"))
                ]
                for finding in review["findings"]:
                    if finding.startswith("failure-rate:"):
                        set_stop(state, "failure-rate", finding)
                    elif finding.startswith(("integrity:", "capability:", "accounting:")):
                        set_stop(state, "integrity", finding)
                    elif finding.startswith("unit-screen:"):
                        set_stop(state, "critical-review-flag", finding)
                if residual and state["stop"] is None:
                    # A failed review that maps to no specific stop still
                    # gates the scope: never silently continue past it.
                    set_stop(state, "review-failed", "; ".join(residual))
                if job["kind"] == "retry":
                    _try_reconcile(state, job_id, job, backend, actions)
        # Retry reconciliation is retried on later steps: a reviewed retry
        # whose reconcile failed (or whose step crashed first) must still
        # merge back into its parent.
        for job_id, job in state["jobs"].items():
            if (
                job["kind"] == "retry"
                and job["stages"].get("reviewed")
                and not job["stages"].get("reconciled")
            ):
                _try_reconcile(state, job_id, job, backend, actions)
        # 3. Refill (retries first, then queued fresh) while slots/budget allow.
        while state["stop"] is None and occupied_slots(state) < MAX_IN_FLIGHT:
            nxt = _next_work(state)
            if nxt is None:
                break
            kind, run_dir, parent_ref = nxt
            parent_job = None
            if kind == "retry-new":
                # Retries are prepared (offline) from the reviewed parent,
                # then submitted as their own child job with its own slot.
                parent_job = state["jobs"][parent_ref]
                try:
                    child_dir = backend.prepare_retry(run_dir)
                except Exception as exc:
                    actions.append(f"retry for {run_dir.name} not preparable ({exc})")
                    parent_job["retry_prepared"] = "none-eligible"
                    break
                parent_job["retry_prepared"] = str(child_dir)
                run_dir = child_dir
                kind = "retry"
            maximum = cap_based_max(run_dir, settings)
            if maximum is None:
                set_stop(
                    state, "budget-unknown", f"{run_dir}: no pricing; cannot reserve conservatively"
                )
                break
            if not budget_ok(state, maximum):
                remaining = round(
                    state["ceiling"] - state["completed_actual"] - reserved_total(state),
                    4,
                )
                set_stop(state, "budget-exhausted", f"remaining {remaining} < {maximum}")
                break
            try:
                batch_id = backend.submit(run_dir)
            except Exception as exc:
                job_id = f"{kind}-{run_dir.name}"
                state["jobs"].setdefault(job_id, _job(kind, run_dir))
                state["jobs"][job_id]["ambiguous"] = True
                state["jobs"][job_id]["reserved_max"] = maximum
                if kind == "retry":
                    state["jobs"][job_id]["parent"] = (
                        str(parent_job["run_dir"]) if parent_job is not None else parent_ref
                    )
                state["queue"] = [i for i in state["queue"] if i["run_dir"] != str(run_dir)]
                actions.append(f"{job_id}: submission ambiguous ({exc}); slot held")
                log(state, actions[-1])
                break
            job_id = f"{kind}-{run_dir.name}"
            state["jobs"][job_id] = {
                **_job(kind, run_dir),
                "batch_id": batch_id,
                "status": "submitted",
                "reserved_max": maximum,
            }
            if kind == "retry":
                state["jobs"][job_id]["parent"] = (
                    str(parent_job["run_dir"]) if parent_job is not None else parent_ref
                )
            state["queue"] = [i for i in state["queue"] if i["run_dir"] != str(run_dir)]
            actions.append(f"{job_id}: submitted {batch_id} reserved={maximum}")
            log(state, actions[-1])
        if state["stop"] is not None:
            _write_stop_report(scope_dir, state)
        _write_progress(scope_dir, state)
        stop = state["stop"]
    return {"actions": actions, "stop": stop}


def _job(kind: str, run_dir: Path) -> dict[str, Any]:
    return {
        "kind": kind,
        "run_dir": str(run_dir),
        "batch_id": None,
        "ambiguous": False,
        "status": "prepared",
        "reserved_max": 0.0,
        "actual": None,
        "stages": {},
    }


def pending_items(state: dict[str, Any]) -> list[str]:
    """Human-readable outstanding work: unaccounted/unreviewed jobs,
    unreconciled retries, unprepared retries of passing fresh parents,
    and queued units. Empty means the scope is complete."""
    pending: list[str] = []
    for job_id, job in state["jobs"].items():
        if not (job.get("batch_id") or job.get("ambiguous")):
            continue
        stages = job.get("stages", {})
        if job.get("ambiguous") or not stages.get("collected"):
            pending.append(f"{job_id}: awaiting collection/accounting")
        elif not stages.get("finalized"):
            pending.append(f"{job_id}: awaiting finalize")
        elif not stages.get("reviewed"):
            pending.append(f"{job_id}: awaiting source review")
        elif job["kind"] == "retry" and not stages.get("reconciled"):
            pending.append(f"{job_id}: awaiting reconcile into parent")
    for job_id, job in state["jobs"].items():
        if (
            job["kind"] == "fresh"
            and job.get("stages", {}).get("reviewed")
            and not job.get("retry_prepared")
            and state["reviews"].get(job_id, {}).get("passed")
        ):
            pending.append(f"{job_id}: passing fresh parent, retry not yet prepared")
    for item in state["queue"]:
        pending.append(f"queued {item['kind']}: {Path(item['run_dir']).name}")
    return pending


def scope_complete(state: dict[str, Any]) -> bool:
    """No completion while reconciliation, accounting, required source
    review, retry preparation, or queued work is pending."""
    return not pending_items(state)


def prior_batch_open(state: dict[str, Any]) -> bool:
    """True while a *completed* batch still needs review/reconcile, or any
    retry unit is queued: a completed batch's slot must not refill with
    new fresh work until its review (and any reconcile) is recorded.
    In-flight (non-terminal) jobs do not block concurrent fresh submits."""
    for job_id, job in state["jobs"].items():
        if not (job.get("batch_id") or job.get("ambiguous")):
            continue
        stages = job.get("stages", {})
        if not stages.get("reviewed"):
            if job.get("ambiguous") or job.get("status") in TERMINAL:
                return True
            continue
        if state["reviews"].get(job_id, {}).get("passed") is not True:
            return True
        if job["kind"] == "retry" and not stages.get("reconciled"):
            return True
    return any(item["kind"] == "retry" for item in state["queue"])


def _try_reconcile(
    state: dict[str, Any],
    job_id: str,
    job: dict[str, Any],
    backend: Any,
    actions: list[str],
) -> None:
    try:
        backend.reconcile_runs(Path(job["run_dir"]))
    except Exception as exc:
        actions.append(f"{job_id}: reconcile failed ({exc})")
        return
    job["stages"]["reconciled"] = True
    actions.append(f"{job_id}: reconciled into parent")
    log(state, actions[-1])


def _next_work(state: dict[str, Any]) -> tuple[str, Path, str | None] | None:
    """Next submittable unit: queued items first (seeded retry-first),
    then prepare-once retries for reviewed-and-passing fresh parents.

    Queue items are {"kind": "fresh"|"retry", "run_dir": ..., "parent"?}.

    A queued fresh unit is releasable only when no prior batch work is
    still open (review/reconcile/queued retry pending).
    """
    if state["queue"]:
        item = state["queue"][0]
        if item["kind"] == "fresh" and prior_batch_open(state):
            return None
        return (item["kind"], Path(item["run_dir"]), item.get("parent"))
    for job_id, job in state["jobs"].items():
        if (
            job["kind"] == "fresh"
            and job["stages"].get("reviewed")
            and not job.get("retry_prepared")
            and state["reviews"].get(job_id, {}).get("passed")
        ):
            job["retry_prepared"] = "preparing"
            return ("retry-new", Path(job["run_dir"]), job_id)
    return None


def _write_progress(scope_dir: Path, state: dict[str, Any]) -> None:
    reviewed = sum(1 for j in state["jobs"].values() if j["stages"].get("reviewed"))
    report = {
        "active_jobs": occupied_slots(state),
        "awaiting_review": sum(
            1
            for j in state["jobs"].values()
            if (j.get("batch_id") or j.get("ambiguous")) and not j["stages"].get("reviewed")
        ),
        "jobs_total": len(state["jobs"]),
        "reviewed": reviewed,
        "actual_spending": state["completed_actual"],
        "reserved_spending": reserved_total(state),
        "remaining_budget": round(
            state["ceiling"] - state["completed_actual"] - reserved_total(state), 4
        ),
        "state": "stopped" if state["stop"] else "running",
        "stop": state["stop"],
        "jobs": {
            job_id: {
                "kind": j["kind"],
                "batch_id": j.get("batch_id"),
                "ambiguous": j.get("ambiguous"),
                "status": j.get("status"),
                "stages": j["stages"],
                "reserved_max": j["reserved_max"],
                "actual": j.get("actual"),
            }
            for job_id, j in state["jobs"].items()
        },
    }
    (scope_dir / "progress.json").write_text(json.dumps(report, indent=1))


def _write_stop_report(scope_dir: Path, state: dict[str, Any]) -> None:
    in_flight: list[str] = []
    pending_review: list[str] = []
    unsubmitted = list(state["queue"])
    for job_id, job in state["jobs"].items():
        if job["stages"].get("reviewed"):
            continue
        (pending_review if job.get("status") in TERMINAL else in_flight).append(job_id)
    (scope_dir / "stop-report.json").write_text(
        json.dumps(
            {
                "stop": state["stop"],
                "completed_reviewed": [
                    j for j, job in state["jobs"].items() if job["stages"].get("reviewed")
                ],
                "in_flight": in_flight,
                "pending_review": pending_review,
                "pending_reconcile": [
                    j
                    for j, job in state["jobs"].items()
                    if job["kind"] == "retry"
                    and job["stages"].get("reviewed")
                    and not job["stages"].get("reconciled")
                ],
                "unsubmitted": unsubmitted,
                "note": "In-flight jobs keep collecting/accounting; results stay "
                "in artifacts/staging only; no application loading.",
            },
            indent=1,
        )
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope-dir", required=True)
    parser.add_argument("--poll-seconds", type=int, default=120)
    parser.add_argument("--max-steps", type=int, default=0)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("step", help="single coordinator iteration")
    sub.add_parser("run", help="unattended loop until scope done/stop/budget out")
    sub.add_parser("status", help="print progress report")
    return parser


def main() -> None:
    from culinary_copilot.config import Settings

    parser = build_parser()
    args = parser.parse_args()
    scope_dir = Path(args.scope_dir)
    settings = Settings()
    if args.command == "status":
        with locked_state(scope_dir):
            pass
        print(
            (scope_dir / "progress.json").read_text()
            if (scope_dir / "progress.json").exists()
            else "no progress yet"
        )
        return
    backend = LiveBackend(settings)
    if args.command == "step":
        print(json.dumps(step(scope_dir, backend, settings), indent=1))
        return
    n = 0
    while True:
        out = step(scope_dir, backend, settings)
        print(json.dumps(out, indent=1))
        n += 1
        with locked_state(scope_dir) as state:
            done = state["stop"] is not None or scope_complete(state)
            if done and state["stop"] is None:
                actions_note = pending_items(state)
                assert not actions_note, actions_note
        if done or (args.max_steps and n >= args.max_steps):
            break
        time.sleep(args.poll_seconds)


if __name__ == "__main__":
    main()
