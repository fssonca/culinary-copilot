"""Mocked scheduler tests: 3-slot window, checkpoint refill, stops, budget, resume.

No network, no model calls, no databases. A scripted FakeBackend drives
llm_sched.step() against synthetic run dirs (review reads only
records/ready_to_load/validation-report artifacts).
"""

import json
from pathlib import Path

from culinary_copilot.recipes import llm_sched


def _manifest(run_dir: Path, requests=2, est_in=1000, cap=8000):
    (run_dir / "manifest.json").write_text(
        json.dumps(
            {
                "stage": "prepared",
                "estimated_input_tokens": est_in,
                "requests": requests,
                "max_output_tokens_per_request": cap,
            }
        )
    )


def _records(run_dir: Path, rows, ready_ids=(), problems=None):
    """rows: [(source_id, state, verdict, attempts)]. Ready rows get clean
    partial capabilities + llm provenance unless problems override."""
    (run_dir / "records.jsonl").write_text(
        "\n".join(
            json.dumps(
                {
                    "source_id": s,
                    "row_number": 1,
                    "state": st,
                    "verdict": v,
                    "attempts": a,
                }
            )
            for s, st, v, a in rows
        )
        + "\n"
    )
    ready = []
    for s, _st, _v, _a in rows:
        if s in ready_ids:
            ready.append(
                {
                    "source_id": s,
                    "title": "T",
                    "description": "A test dish.",
                    "ingredients": [
                        {
                            "name": "salt",
                            "unit": None,
                            "original": "salt",
                            "source_line_id": "L1",
                        }
                    ],
                    "instructions": ["Mix."],
                    "capabilities": {},
                    "scalable": False,
                    "final_recipe_eligible": False,
                    "llm_status": "partially_resolved",
                    "provenance": {"llm": {"model_requested": "m"}},
                }
            )
    (run_dir / "ready_to_load.jsonl").write_text("\n".join(json.dumps(r) for r in ready) + "\n")
    (run_dir / "validation-report.json").write_text(json.dumps({"validations": problems or []}))


class FakeBackend:
    def __init__(self):
        self.statuses = {}
        self.submitted = []
        self.fail_submit = set()
        self.ambiguous_once = set()
        self.ambiguous_seen = set()
        self.fail_finalize = set()
        self.reconciled = []
        self.retries_prepared = []

    def submit(self, run_dir: Path):
        name = run_dir.name
        if name in self.fail_submit and name not in self.ambiguous_seen:
            self.ambiguous_seen.add(name)
            raise RuntimeError("connection reset during batch create")
        self.submitted.append(name)
        self.statuses[name] = "in_progress"
        return f"batch-{name}"

    def query(self, run_dir: Path):
        return self.statuses.get(run_dir.name, "in_progress")

    def reconcile_submission(self, run_dir: Path):
        name = run_dir.name
        if name in self.ambiguous_seen:
            self.statuses[name] = "in_progress"
            return f"batch-{name}"
        return None

    def collect(self, run_dir: Path):
        return {"ok": 1, "failed": 0, "estimated_actual_cost_usd": 0.001}

    def finalize(self, run_dir: Path):
        if run_dir.name in self.fail_finalize:
            raise RuntimeError("disk hiccup")
        return {"ready": 1}

    def prepare_retry(self, parent_dir: Path):
        child = parent_dir / "retry-kid"
        child.mkdir(exist_ok=True)
        _manifest(child, requests=1)
        self.retries_prepared.append(parent_dir.name)
        return child

    def reconcile_runs(self, child_dir: Path):
        self.reconciled.append(child_dir.name)
        return {}


class Settings:
    llm_price_input_per_1m = 0.05
    llm_price_output_per_1m = 0.40


def _scope(tmp_path: Path, queue):
    scope = tmp_path / "sched"
    scope.mkdir()
    absolute = []
    for item in queue:
        d = tmp_path / item["run_dir"]
        d.mkdir(exist_ok=True)
        _manifest(d)
        absolute.append({**item, "run_dir": str(d)})
    with llm_sched.locked_state(scope) as state:
        state["queue"] = absolute
    return scope


def test_max_three_in_flight(tmp_path):
    backend = FakeBackend()
    scope = _scope(tmp_path, [{"kind": "fresh", "run_dir": f"r{i}"} for i in range(5)])
    llm_sched.step(scope, backend, Settings())
    assert backend.submitted == ["r0", "r1", "r2"]
    with llm_sched.locked_state(scope) as state:
        assert llm_sched.occupied_slots(state) == 3
        assert len(state["queue"]) == 2


def test_retry_consumes_same_slots(tmp_path):
    backend = FakeBackend()
    scope = _scope(
        tmp_path,
        [{"kind": "retry", "run_dir": "c0", "parent": "p0"}]
        + [{"kind": "fresh", "run_dir": f"r{i}"} for i in range(4)],
    )
    llm_sched.step(scope, backend, Settings())
    assert backend.submitted == ["c0", "r0", "r1"]
    with llm_sched.locked_state(scope) as state:
        assert state["jobs"]["retry-c0"]["parent"] == "p0"
        assert llm_sched.occupied_slots(state) == 3


def test_terminal_unreviewed_holds_its_slot_on_finalize_failure(tmp_path):
    backend = FakeBackend()
    backend.statuses.update({"r0": "completed", "r1": "in_progress", "r2": "in_progress"})
    backend.fail_finalize.add("r0")
    scope = _scope(tmp_path, [{"kind": "fresh", "run_dir": "r3"}])
    run0 = tmp_path / "r0"
    run0.mkdir(exist_ok=True)
    _manifest(run0)
    with llm_sched.locked_state(scope) as state:
        for name in ("r0", "r1", "r2"):
            d = tmp_path / name
            if name != "r0":
                d.mkdir(exist_ok=True)
                _manifest(d)
            state["jobs"][f"fresh-{name}"] = {
                **llm_sched._job("fresh", d),
                "batch_id": f"batch-{name}",
                "status": backend.statuses[name],
                "reserved_max": 0.01,
            }
    out = llm_sched.step(scope, backend, Settings())
    assert any("finalize failed" in a for a in out["actions"])
    assert backend.submitted == []  # all 3 slots held; r3 waits
    with llm_sched.locked_state(scope) as state:
        assert llm_sched.occupied_slots(state) == 3


def test_job_failure_stops_new_submits_others_continue(tmp_path):
    backend = FakeBackend()
    backend.statuses.update({"r0": "completed", "r1": "in_progress"})
    scope = _scope(tmp_path, [{"kind": "fresh", "run_dir": "r2"}])
    for name, bad in (("r0", True), ("r1", False)):
        d = tmp_path / name
        d.mkdir(exist_ok=True)
        _manifest(d)
        rows = (
            [(f"s{i}", "unresolved", "rejected_validation", 1) for i in range(8)]
            + [(f"ok{i}", "ready_to_load", "accepted_partial", 1) for i in range(2)]
            if bad
            else [(f"ok{i}", "ready_to_load", "accepted_partial", 1) for i in range(2)]
        )
        _records(d, rows, ready_ids=[f"ok{i}" for i in range(2)])
    with llm_sched.locked_state(scope) as state:
        for name in ("r0", "r1"):
            d = tmp_path / name
            state["jobs"][f"fresh-{name}"] = {
                **llm_sched._job("fresh", d),
                "batch_id": f"batch-{name}",
                "status": "completed" if name == "r0" else "in_progress",
                "reserved_max": 0.01,
            }
    out = llm_sched.step(scope, backend, Settings())
    assert out["stop"] and out["stop"]["reason"] == "failure-rate"
    assert backend.submitted == []  # r2 never submitted
    with llm_sched.locked_state(scope) as state:
        assert state["jobs"]["fresh-r1"]["status"] == "in_progress"  # still tracked
        assert (scope / "stop-report.json").exists()


def test_ambiguous_submission_never_duplicates(tmp_path):
    backend = FakeBackend()
    backend.fail_submit.add("r0")
    scope = _scope(tmp_path, [{"kind": "fresh", "run_dir": "r0"}])
    llm_sched.step(scope, backend, Settings())  # submit raises -> ambiguous
    with llm_sched.locked_state(scope) as state:
        assert state["jobs"]["fresh-r0"]["ambiguous"] is True
        assert state["queue"] == []  # dequeued; slot held, never resubmitted
    out = llm_sched.step(scope, backend, Settings())  # reconcile adopts
    assert any("reconciled to batch-r0" in a for a in out["actions"])
    assert backend.submitted == []
    llm_sched.step(scope, backend, Settings())  # adopted job polls normally
    assert backend.submitted == []
    assert backend.submitted == []
    with llm_sched.locked_state(scope) as state:
        job = state["jobs"]["fresh-r0"]
        assert job["batch_id"] == "batch-r0" and job["ambiguous"] is False


def test_budget_reservations_enforce_ceiling(tmp_path):
    backend = FakeBackend()
    scope = _scope(
        tmp_path,
        [{"kind": "fresh", "run_dir": f"r{i}"} for i in range(2)],
    )
    with llm_sched.locked_state(scope) as state:
        state["ceiling"] = 0.001  # below any cap-based reservation
    out = llm_sched.step(scope, backend, Settings())
    assert out["stop"] and out["stop"]["reason"] == "budget-exhausted"
    assert backend.submitted == []
    with llm_sched.locked_state(scope) as state:
        assert state["completed_actual"] + llm_sched.reserved_total(state) <= 0.001


def test_restart_preserves_state_and_prevents_duplicates(tmp_path):
    backend = FakeBackend()
    backend.statuses["r0"] = "completed"
    scope = _scope(tmp_path, [{"kind": "fresh", "run_dir": "r1"}])
    d = tmp_path / "r0"
    d.mkdir(exist_ok=True)
    _manifest(d)
    _records(
        d,
        [(f"ok{i}", "ready_to_load", "accepted_partial", 1) for i in range(2)],
        ready_ids=["ok0", "ok1"],
    )
    with llm_sched.locked_state(scope) as state:
        state["jobs"]["fresh-r0"] = {
            **llm_sched._job("fresh", d),
            "batch_id": "batch-r0",
            "status": "in_progress",
            "reserved_max": 0.05,
        }
    # Step 1 collects/reviews r0, submits queued r1, then prepares + submits
    # r0's retry (one slot each; retries prioritized after passing review).
    llm_sched.step(scope, backend, Settings())
    assert backend.submitted == ["r1", "retry-kid"]
    assert backend.retries_prepared == ["r0"]
    # "restart": fresh lock, same backend; nothing new is submitted twice.
    backend.statuses["r1"] = "in_progress"
    backend.statuses["retry-kid"] = "in_progress"
    llm_sched.step(scope, backend, Settings())
    llm_sched.step(scope, backend, Settings())
    assert backend.submitted == ["r1", "retry-kid"]  # no duplicate submits
    with llm_sched.locked_state(scope) as state:
        assert state["jobs"]["fresh-r0"]["stages"]["reviewed"] is True
        assert state["completed_actual"] == 0.001
        assert state["jobs"]["fresh-r0"]["reserved_max"] == 0.0
        assert state["jobs"]["retry-retry-kid"]["parent"].endswith("r0")
        assert state["stop"] is None


def _padded_run(tmp_path: Path, names):
    from culinary_copilot.recipes.llm_batch import _write_jsonl

    run_dir = tmp_path / "run"
    run_dir.mkdir(exist_ok=True)
    row = {
        "source_id": "foodie-000001",
        "title": "T",
        "description": "A test dish.",
        "ingredients": [
            {
                "name": n,
                "unit": None,
                "original": n,
                "source_line_id": "L4",
            }
            for n in names
        ],
        "instructions": ["Mix."],
        "capabilities": {},
        "scalable": False,
        "final_recipe_eligible": False,
        "llm_status": "partially_resolved",
        "provenance": {"llm": {"model_requested": "m"}},
    }
    _write_jsonl(run_dir / "ready_to_load.jsonl", [row])
    _records(
        run_dir,
        [("foodie-000001", "ready_to_load", "accepted_partial", 1)],
        ready_ids=(),
    )
    _write_jsonl(run_dir / "ready_to_load.jsonl", [row])
    return run_dir


def test_padding_repetition_surfaced_without_stopping(tmp_path):
    """Repeated names are close-read candidates, never an automated stop:
    honest component repetition (000185) is structurally identical to
    junk padding (16331-class), so humans decide."""
    run_dir = _padded_run(tmp_path, ["Cauliflower"] * 4)
    review = llm_sched.review_job(run_dir, "fresh")
    assert review["passed"] is True
    assert review["padding_candidates"] == ["foodie-000001: 'cauliflower' x4"]


def test_padding_screen_allows_distinct_glued_line_items(tmp_path):
    names = ["pineapple", "vanilla greek yogurt", "banana", "ice", "fresh ginger", "water"]
    run_dir = _padded_run(tmp_path, names)
    review = llm_sched.review_job(run_dir, "fresh")
    assert review["passed"] is True
    assert review["padding_candidates"] == []


def test_reconcile_retried_until_success(tmp_path):
    class Flaky(FakeBackend):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def reconcile_runs(self, child_dir: Path):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("parent busy")
            return super().reconcile_runs(child_dir)

    backend = Flaky()
    scope = _scope(tmp_path, [])
    child = tmp_path / "c0"
    child.mkdir(exist_ok=True)
    _manifest(child, requests=1)
    _records(child, [("s0", "ready_to_load", "accepted", 1)], ready_ids=["s0"])
    (child / "manifest.json").write_text(
        json.dumps({**json.loads((child / "manifest.json").read_text()), "parent_run": "p0"})
    )
    with llm_sched.locked_state(scope) as state:
        state["jobs"]["retry-c0"] = {
            **llm_sched._job("retry", child),
            "batch_id": "batch-c0",
            "parent": "p0",
            "reserved_max": 0.0,
            "stages": {"collected": True, "finalized": True, "reviewed": True},
        }
        state["reviews"]["retry-c0"] = {"passed": True}
    llm_sched.step(scope, backend, Settings())
    llm_sched.step(scope, backend, Settings())
    assert backend.reconciled == ["c0"]
    with llm_sched.locked_state(scope) as state:
        assert state["jobs"]["retry-c0"]["stages"]["reconciled"] is True


def test_backend_args_accept_str_and_path(tmp_path):
    """Manifest-sourced parents arrive as str; run dirs as Path — both
    must build runnable args (live reconcile bug class)."""
    backend = llm_sched.LiveBackend(settings=None)
    for given in (tmp_path, str(tmp_path)):
        args = backend._args(given)
        assert Path(args.cache_dir) == tmp_path / "cache"


def test_rejection_threshold_exact_thirty_three_percent():
    # Exact rule: 100*rejected > 33*fresh_records.
    assert llm_sched.rejection_breach(49, 150) is False
    assert llm_sched.rejection_breach(50, 150) is True
    assert llm_sched.rejection_breach(66, 200) is False
    assert llm_sched.rejection_breach(67, 200) is True
    # Other sizes: exact third boundaries (33.33…%) breach; below passes.
    assert llm_sched.rejection_breach(0, 3) is False
    assert llm_sched.rejection_breach(1, 3) is True  # 100 > 99
    assert llm_sched.rejection_breach(2, 3) is True
    assert llm_sched.rejection_breach(0, 1) is False
    assert llm_sched.rejection_breach(1, 1) is True
    assert llm_sched.rejection_breach(33, 100) is False
    assert llm_sched.rejection_breach(34, 100) is True
    assert llm_sched.rejection_breach(6, 20) is False  # 30%
    assert llm_sched.rejection_breach(7, 20) is True  # 35%: 700 > 660
    assert llm_sched.rejection_breach(0, 0) is False


def test_u2028_in_merged_row_survives_jsonl_round_trip(tmp_path):
    """Row-15599 class: raw U+2028 inside a merged field must not corrupt
    JSONL parsing (splitlines splits on U+2028; the production reader
    splits on newline only)."""
    from culinary_copilot.recipes.llm_batch import _read_jsonl, _write_jsonl

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    row = {
        "source_id": "foodie-015599",
        "title": "T",
        "description": "A test dish.",
        "ingredients": [
            {
                "name": "cheese , grated",
                "unit": None,
                "original": "cheese, grated",
                "source_line_id": "L1",
            }
        ],
        "instructions": ["Mix."],
        "capabilities": {},
        "scalable": False,
        "final_recipe_eligible": False,
        "llm_status": "partially_resolved",
        "provenance": {"llm": {"model_requested": "m"}},
    }
    _write_jsonl(run_dir / "ready_to_load.jsonl", [row])
    assert _read_jsonl(run_dir / "ready_to_load.jsonl") == [row]
    _records(
        run_dir,
        [("foodie-015599", "ready_to_load", "accepted_partial", 1)],
        ready_ids=(),
    )
    _write_jsonl(run_dir / "ready_to_load.jsonl", [row])
    review = llm_sched.review_job(run_dir, "fresh")
    assert review["ready"] == 1
    assert review["passed"] is True


def test_scope_not_complete_while_reconcile_or_review_pending(tmp_path):
    scope = _scope(tmp_path, [])
    child = tmp_path / "c0"
    child.mkdir(exist_ok=True)
    _manifest(child, requests=1)
    with llm_sched.locked_state(scope) as state:
        # Reviewed retry without reconcile blocks completion.
        state["jobs"]["retry-c0"] = {
            **llm_sched._job("retry", child),
            "batch_id": "batch-c0",
            "status": "completed",
            "reserved_max": 0.0,
            "stages": {"collected": True, "finalized": True, "reviewed": True},
        }
        state["reviews"]["retry-c0"] = {"passed": True}
        assert llm_sched.scope_complete(state) is False
        assert any("reconcile" in p for p in llm_sched.pending_items(state))
        # Unreviewed terminal job blocks completion too.
        run = tmp_path / "r0"
        run.mkdir(exist_ok=True)
        _manifest(run)
        state["jobs"]["fresh-r0"] = {
            **llm_sched._job("fresh", run),
            "batch_id": "batch-r0",
            "status": "completed",
            "reserved_max": 0.0,
            "stages": {"collected": True},
        }
        assert llm_sched.scope_complete(state) is False


def test_scope_complete_when_fully_accounted(tmp_path):
    scope = _scope(tmp_path, [])
    with llm_sched.locked_state(scope) as state:
        assert llm_sched.scope_complete(state) is True
        assert llm_sched.pending_items(state) == []


def test_fresh_submit_waits_for_completed_batch_review(tmp_path):
    from culinary_copilot.recipes.llm_batch import _write_jsonl

    backend = FakeBackend()
    backend.statuses["r0"] = "completed"
    scope = _scope(tmp_path, [{"kind": "fresh", "run_dir": "r1"}])
    run = tmp_path / "r0"
    run.mkdir(exist_ok=True)
    _manifest(run)
    # r0 is terminal with a coverage hole: review fails -> scope stops,
    # r1 (next batch) is never submitted.
    _records(run, [("s0", "ready_to_load", "accepted_partial", 1)], ready_ids=["s0"])
    _write_jsonl(
        run / "ready_to_load.jsonl",
        [
            {
                "source_id": "s0",
                "title": "T",
                "description": "",
                "ingredients": [],
                "instructions": [],
                "capabilities": {},
                "scalable": False,
                "final_recipe_eligible": False,
                "llm_status": "partially_resolved",
                "provenance": {"llm": {"model_requested": "m"}},
            }
        ],
    )
    with llm_sched.locked_state(scope) as state:
        state["jobs"]["fresh-r0"] = {
            **llm_sched._job("fresh", run),
            "batch_id": "batch-r0",
            "status": "completed",  # terminal but unreviewed
            "reserved_max": 0.0,
            "stages": {"collected": True, "finalized": True},
        }
        assert llm_sched.prior_batch_open(state) is True
    out = llm_sched.step(scope, backend, Settings())
    assert backend.submitted == []  # r1 waits; the failed review gates refill
    assert out["stop"] and out["stop"]["reason"] == "review-failed"
    with llm_sched.locked_state(scope) as state:
        assert state["reviews"]["fresh-r0"]["passed"] is False
        assert llm_sched.prior_batch_open(state) is True  # failed review still gates


def test_fresh_submit_allowed_while_other_batch_in_flight(tmp_path):
    backend = FakeBackend()
    scope = _scope(tmp_path, [{"kind": "fresh", "run_dir": "r1"}])
    run = tmp_path / "r0"
    run.mkdir(exist_ok=True)
    _manifest(run)
    with llm_sched.locked_state(scope) as state:
        state["jobs"]["fresh-r0"] = {
            **llm_sched._job("fresh", run),
            "batch_id": "batch-r0",
            "status": "in_progress",  # still running: no completed batch open
            "reserved_max": 0.01,
        }
        assert llm_sched.prior_batch_open(state) is False
    llm_sched.step(scope, backend, Settings())
    assert backend.submitted == ["r1"]


def test_scheduler_snapshot_refresh_keeps_extraction_frozen(tmp_path):
    backend = FakeBackend()
    scope = _scope(tmp_path, [])
    with llm_sched.locked_state(scope) as state:
        frozen = {k: v for k, v in llm_sched.current_versions().items() if k != "scheduler_version"}
        state["versions"] = frozen  # pre-fix snapshot without the new key
    out = llm_sched.step(scope, backend, Settings())
    assert out["stop"] is None  # refreshed, not stopped
    with llm_sched.locked_state(scope) as state:
        assert state["versions"] == llm_sched.current_versions()
        assert any("snapshot refreshed" in e["message"] for e in state["events"])


def test_extraction_change_still_stops(tmp_path):
    backend = FakeBackend()
    scope = _scope(tmp_path, [])
    with llm_sched.locked_state(scope) as state:
        tampered = dict(llm_sched.current_versions())
        tampered["prompt_version"] = "v0-tampered"
        state["versions"] = tampered
    out = llm_sched.step(scope, backend, Settings())
    assert out["stop"] and out["stop"]["reason"] == "version-change"


def test_review_screens_all_rows_and_lists_converted(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir(exist_ok=True)
    rows = [(f"s{i}", "ready_to_load", "accepted_partial", 1) for i in range(35)]
    rows.append(("conv", "ready_to_load", "accepted_partial", 2))
    rows.append(("bad", "unresolved", "rejected_validation", 1))
    _records(run_dir, rows, ready_ids=[f"s{i}" for i in range(35)] + ["conv"])
    review = llm_sched.review_job(run_dir, "fresh")
    assert review["passed"] is True
    assert review["ready"] == 36
    assert review["converted"] == ["conv"]
    assert len(review["manual_review_records"]) == 31  # first 30 + converted
    assert "conv" in review["manual_review_records"]
    # Exact rule: 1/36 ~= 2.8% does not breach.
    assert review["rejected_first"] == 1 and review["total_first"] == 36
    assert not any(f.startswith("failure-rate:") for f in review["findings"])


def test_review_flags_empty_coverage(tmp_path):
    from culinary_copilot.recipes.llm_batch import _write_jsonl

    run_dir = tmp_path / "run"
    run_dir.mkdir(exist_ok=True)
    _records(run_dir, [("s0", "ready_to_load", "accepted_partial", 1)], ready_ids=["s0"])
    _write_jsonl(
        run_dir / "ready_to_load.jsonl",
        [
            {
                "source_id": "s0",
                "title": "T",
                "description": "",
                "ingredients": [],
                "instructions": [],
                "capabilities": {},
                "scalable": False,
                "final_recipe_eligible": False,
                "llm_status": "partially_resolved",
                "provenance": {"llm": {"model_requested": "m"}},
            }
        ],
    )
    review = llm_sched.review_job(run_dir, "fresh")
    assert review["passed"] is False
    assert any(f.startswith("coverage:") for f in review["findings"])


def test_missing_description_is_note_not_stop(tmp_path):
    from culinary_copilot.recipes.llm_batch import _write_jsonl

    run_dir = tmp_path / "run"
    run_dir.mkdir(exist_ok=True)
    _records(run_dir, [("s0", "ready_to_load", "accepted_partial", 1)], ready_ids=["s0"])
    _write_jsonl(
        run_dir / "ready_to_load.jsonl",
        [
            {
                "source_id": "s0",
                "title": "T",
                "description": None,
                "ingredients": [
                    {
                        "name": "salt",
                        "unit": None,
                        "original": "salt",
                        "source_line_id": "L1",
                    }
                ],
                "instructions": ["Mix."],
                "capabilities": {},
                "scalable": False,
                "final_recipe_eligible": False,
                "llm_status": "partially_resolved",
                "provenance": {"llm": {"model_requested": "m"}},
            }
        ],
    )
    review = llm_sched.review_job(run_dir, "fresh")
    assert review["passed"] is True
    assert review["coverage_notes"] == ["s0: ready without description"]
