"""Bounded, concurrency-safe in-memory development store.

State ownership (documented, minimal): one process-local store guarded by a
threading lock, capped at ``max_entries`` requests (FIFO eviction). Restart
loss and single-worker scope are accepted limitations; the storage boundary
(``ClarificationStore`` protocol) is replaceable for later persistence
without changing the API or planning logic. No PostgreSQL tables or
migrations are added here. Client-supplied state is never trusted: the
server is authoritative and rejects stale revisions.

Concurrency contract (single process):

- Reads return deep copies: mutating a returned object never affects storage.
- Mutations commit through compare-and-swap: ``commit_answers`` and
  ``commit_replan`` succeed only when the stored revisions still match the
  caller's snapshot, so two same-revision submissions cannot both succeed and
  a stale replan cannot overwrite newer answers. The losers get ``False``
  (surfaced as HTTP 409).
- No lock is ever held across a network call: handlers snapshot, release the
  lock, run validation/planning (including provider calls), then CAS-commit.
- A per-request registry of every issued question is kept so typed edits to
  earlier questions keep working after the pending group is filtered.
"""

from __future__ import annotations

import threading
import uuid
from collections import OrderedDict
from typing import Protocol

from culinary_copilot.domain.clarification import (
    CookingRequestState,
    Question,
    QuestionGroup,
)


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class ClarificationStore(Protocol):
    def create(self, state: CookingRequestState, group: QuestionGroup) -> None: ...
    def get_state(self, request_id: str) -> CookingRequestState | None: ...
    def get_group(self, group_id: str) -> tuple[CookingRequestState, QuestionGroup] | None: ...
    def get_snapshot(self, group_id: str) -> tuple[CookingRequestState, QuestionGroup] | None: ...
    def groups_for_request(self, request_id: str) -> list[QuestionGroup]: ...
    def current_group_id(self, request_id: str) -> str | None: ...
    def known_questions(self, request_id: str) -> list[Question]: ...
    def save_state(self, state: CookingRequestState) -> None: ...
    def save_group(self, group: QuestionGroup) -> None: ...
    def commit_answers(
        self,
        request_id: str,
        *,
        expected_state_rev: int,
        expected_group_rev: int,
        new_state: CookingRequestState,
        new_group: QuestionGroup,
    ) -> bool: ...
    def commit_replan(
        self,
        request_id: str,
        *,
        expected_state_rev: int,
        new_state: CookingRequestState,
        new_group: QuestionGroup,
    ) -> bool: ...


class InMemoryClarificationStore:
    def __init__(self, max_entries: int = 512) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be >= 1")
        self.max_entries = max_entries
        self._lock = threading.Lock()
        self._states: OrderedDict[str, CookingRequestState] = OrderedDict()
        self._groups: dict[str, QuestionGroup] = {}
        self._group_to_request: dict[str, str] = {}
        self._known: dict[str, dict[str, Question]] = {}
        self._current: dict[str, str] = {}

    def _register(self, request_id: str, group: QuestionGroup) -> None:
        registry = self._known.setdefault(request_id, {})
        for question in group.questions:
            registry.setdefault(question.id, question.model_copy(deep=True))

    def create(self, state: CookingRequestState, group: QuestionGroup) -> None:
        with self._lock:
            self._states[state.request_id] = state.model_copy(deep=True)
            self._states.move_to_end(state.request_id)
            while len(self._states) > self.max_entries:
                evicted_id, _ = self._states.popitem(last=False)
                for gid in [g for g, rid in self._group_to_request.items() if rid == evicted_id]:
                    self._groups.pop(gid, None)
                    self._group_to_request.pop(gid, None)
                self._known.pop(evicted_id, None)
                self._current.pop(evicted_id, None)
            self._groups[group.group_id] = group.model_copy(deep=True)
            self._group_to_request[group.group_id] = state.request_id
            self._register(state.request_id, group)
            self._current[state.request_id] = group.group_id

    def current_group_id(self, request_id: str) -> str | None:
        """Newest group issued for the request; older groups stay readable history."""
        with self._lock:
            return self._current.get(request_id)

    def get_state(self, request_id: str) -> CookingRequestState | None:
        with self._lock:
            state = self._states.get(request_id)
            return state.model_copy(deep=True) if state is not None else None

    def get_group(self, group_id: str) -> tuple[CookingRequestState, QuestionGroup] | None:
        return self.get_snapshot(group_id)

    def get_snapshot(self, group_id: str) -> tuple[CookingRequestState, QuestionGroup] | None:
        """Deep-copied (state, group) pair; safe to mutate outside the lock."""
        with self._lock:
            group = self._groups.get(group_id)
            if group is None:
                return None
            state = self._states.get(group.request_id)
            if state is None:
                return None
            return state.model_copy(deep=True), group.model_copy(deep=True)

    def groups_for_request(self, request_id: str) -> list[QuestionGroup]:
        with self._lock:
            return [
                group.model_copy(deep=True)
                for group in self._groups.values()
                if group.request_id == request_id
            ]

    def known_questions(self, request_id: str) -> list[Question]:
        """Every question ever issued for the request (copies)."""
        with self._lock:
            return [
                question.model_copy(deep=True)
                for question in self._known.get(request_id, {}).values()
            ]

    def save_state(self, state: CookingRequestState) -> None:
        with self._lock:
            self._states[state.request_id] = state.model_copy(deep=True)
            self._states.move_to_end(state.request_id)

    def save_group(self, group: QuestionGroup) -> None:
        with self._lock:
            self._groups[group.group_id] = group.model_copy(deep=True)
            self._group_to_request[group.group_id] = group.request_id
            self._register(group.request_id, group)

    def commit_answers(
        self,
        request_id: str,
        *,
        expected_state_rev: int,
        expected_group_rev: int,
        new_state: CookingRequestState,
        new_group: QuestionGroup,
    ) -> bool:
        """Atomic compare-and-swap for answer submissions.

        Returns True and stores the update only when both stored revisions
        still match the caller's snapshot; otherwise returns False and stores
        nothing (caller surfaces 409).
        """
        with self._lock:
            stored_state = self._states.get(request_id)
            stored_group = self._groups.get(new_group.group_id)
            if stored_state is None or stored_group is None:
                return False
            if (
                stored_state.revision != expected_state_rev
                or stored_group.revision != expected_group_rev
            ):
                return False
            self._states[request_id] = new_state.model_copy(deep=True)
            self._states.move_to_end(request_id)
            self._groups[new_group.group_id] = new_group.model_copy(deep=True)
            self._register(request_id, new_group)
            return True

    def commit_replan(
        self,
        request_id: str,
        *,
        expected_state_rev: int,
        new_state: CookingRequestState,
        new_group: QuestionGroup,
    ) -> bool:
        """Atomic commit for replans: applies only when no newer write landed
        while planning (including its provider call) was running. The snapshot
        (planning conclusions, pending confirmations, replan count) is stored
        with an advanced revision, so the new group supersedes pending
        submissions and in-flight same-revision writes fail closed with 409.
        """
        with self._lock:
            stored_state = self._states.get(request_id)
            if stored_state is None:
                return False
            if stored_state.revision != expected_state_rev:
                return False
            committed = new_state.model_copy(deep=True)
            committed.revision = expected_state_rev + 1
            self._states[request_id] = committed
            self._states.move_to_end(request_id)
            self._groups[new_group.group_id] = new_group.model_copy(deep=True)
            self._group_to_request[new_group.group_id] = request_id
            self._register(request_id, new_group)
            self._current[request_id] = new_group.group_id
            return True
