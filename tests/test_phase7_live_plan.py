"""The live plan command cannot drift from the runner (offline, no calls)."""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "evals" / "phase3_agent"))

import live_run  # noqa: E402

PLAN = REPO_ROOT / "evals" / "phase7_agent" / "LIVE_PLAN.md"

REMOVED_FLAGS = (
    "--max-model-turns",
    "--max-searches-per-scenario",
    "--campaign-search-cap",
    "--search-estimate-usd",
    "--technique-compare",
)


def _command_block() -> str:
    text = PLAN.read_text(encoding="utf-8")
    blocks: list[str] = []
    in_block = False
    current: list[str] = []
    for line in text.splitlines():
        if line.strip().startswith("```"):
            if in_block:
                blocks.append("\n".join(current))
                current = []
                in_block = False
            else:
                in_block = True
            continue
        if in_block:
            current.append(line)
    for block in blocks:
        if "live_run.py" in block:
            return block
    raise AssertionError("no live_run.py command block in LIVE_PLAN.md")


def test_plan_command_parses_and_matches_scenarios() -> None:
    block = _command_block()
    for flag in REMOVED_FLAGS:
        assert flag not in block, flag
    argv = shlex.split(block.replace("\\\n", " "))
    assert argv[:4] == [
        "uv",
        "run",
        "python",
        "evals/phase3_agent/live_run.py",
    ], argv[:4]
    # The runner's own parser: unknown flags fail here, not at run time.
    args = live_run._args(argv[4:])
    assert args.budget_pool == "phase7"
    assert int(args.max_attempts) == 1
    assert int(args.search_max_per_live_session) == 1
    assert int(args.max_campaign_searches or 0) == 1
    scenarios_path = REPO_ROOT / str(args.scenarios_file)
    payload = live_run.load_scenarios(scenarios_path)
    available = {s.get("key") for s in payload.get("scenarios", [])}
    wanted = [k for k in str(args.scenarios or "").split(",") if k]
    assert len(wanted) == 7, wanted
    assert set(wanted) <= available, set(wanted) - available
