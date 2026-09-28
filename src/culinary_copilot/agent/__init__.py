"""Bounded agent loop (Milestone 3, Phase 3).

Hand-written loop over the Phase 2 tool registry with Postgres session
state. See ``agent/loop.py`` and ``docs/agent.md``.
"""

from culinary_copilot.agent.loop import (
    EPICURE_SKIP_ALLOWLIST,
    AgentConcurrentError,
    AgentDeps,
    AgentDirective,
    AgentLoopError,
    AgentRunResult,
    record_answer,
    record_select,
    run_agent,
)

__all__ = [
    "EPICURE_SKIP_ALLOWLIST",
    "AgentConcurrentError",
    "AgentDeps",
    "AgentDirective",
    "AgentLoopError",
    "AgentRunResult",
    "record_answer",
    "record_select",
    "run_agent",
]
