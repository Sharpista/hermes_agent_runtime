"""Adapters for turning Hermes Kanban task completion into runtime results.

This module intentionally does not spawn Hermes workers or reimplement the Kanban
SQLite dispatcher. The installed dispatcher remains the owner of claim/spawn/run
bookkeeping; callers inject a small starter callback and a read-only task snapshot
callback so this adapter can wait for a durable task outcome before returning an
ExecutionResult to AgentRuntime.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
import time
from typing import Callable, Mapping, Protocol

from .runtime import ExecutionBlocked, ExecutionContext, ExecutionResult

logger = logging.getLogger(__name__)

TERMINAL_DONE_STATUSES = frozenset({"done"})
TERMINAL_BLOCKED_STATUSES = frozenset({"blocked"})
TERMINAL_FAILED_OUTCOMES = frozenset({"failed", "timed_out", "spawn_failed", "rate_limited"})

_ALLOWED_RESULT_FIELDS = frozenset({
    "commit_sha",
    "pull_request_url",
    "railway_deployment_id",
    "tests_status",
    "review_status",
})


@dataclass(frozen=True)
class KanbanRunSnapshot:
    """Read-only view of the latest Kanban run for a task."""

    status: str | None = None
    outcome: str | None = None
    summary: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class KanbanTaskSnapshot:
    """Read-only task state required by KanbanDispatcherAdapter."""

    task_id: str
    status: str
    assignee: str | None = None
    workspace_path: str | None = None
    branch: str | None = None
    result: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)
    latest_run: KanbanRunSnapshot | None = None
    child_statuses: Mapping[str, str] = field(default_factory=dict)


class KanbanTaskReader(Protocol):
    def __call__(self, task_id: str) -> KanbanTaskSnapshot: ...


class KanbanDispatchStarter(Protocol):
    def __call__(self, context: ExecutionContext) -> str | KanbanTaskSnapshot: ...


class KanbanDispatcherAdapter:
    """Callable AgentRuntime dispatch adapter backed by the real Kanban dispatcher.

    ``start`` must hand work to the installed Hermes dispatcher and return the
    Kanban task id (or an initial snapshot). A successful spawn is not considered
    success: the adapter polls ``read_task`` until the task reaches a terminal
    task status, then maps verified metadata into ``ExecutionResult``.
    """

    def __init__(
        self,
        start: KanbanDispatchStarter,
        read_task: KanbanTaskReader,
        *,
        poll_seconds: float = 5.0,
        timeout_seconds: float = 3600.0,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        log: logging.Logger | None = None,
    ) -> None:
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._start = start
        self._read_task = read_task
        self._poll_seconds = poll_seconds
        self._timeout_seconds = timeout_seconds
        self._sleep = sleep
        self._monotonic = monotonic
        self._log = log or logger

    def __call__(self, context: ExecutionContext) -> ExecutionResult:
        initial = self._start(context)
        snapshot = initial if isinstance(initial, KanbanTaskSnapshot) else self._read_task(initial)
        task_id = snapshot.task_id
        self._log.info(
            "kanban dispatch started",
            extra={
                "run_id": context.run_id,
                "linear_issue_id": context.linear_issue_id,
                "agent": context.agent,
                "kanban_task_id": task_id,
                "status": snapshot.status,
            },
        )

        events: list[tuple[str, Mapping[str, object]]] = [
            ("kanban.dispatched", self._event_payload(context, snapshot)),
        ]
        deadline = self._monotonic() + self._timeout_seconds
        last_status = snapshot.status

        while snapshot.status not in TERMINAL_DONE_STATUSES | TERMINAL_BLOCKED_STATUSES:
            latest = snapshot.latest_run
            if latest and latest.outcome in TERMINAL_FAILED_OUTCOMES:
                events.append((
                    "kanban.failed",
                    self._event_payload(context, snapshot, outcome=latest.outcome),
                ))
                raise RuntimeError("Kanban worker failed before task completion")
            if self._monotonic() >= deadline:
                events.append(("kanban.timeout", self._event_payload(context, snapshot)))
                raise TimeoutError("Kanban task did not reach a terminal state in time")
            self._sleep(self._poll_seconds)
            snapshot = self._read_task(task_id)
            if snapshot.status != last_status:
                last_status = snapshot.status
                events.append(("kanban.status_changed", self._event_payload(context, snapshot)))
                self._log.info(
                    "kanban task status changed",
                    extra={
                        "run_id": context.run_id,
                        "linear_issue_id": context.linear_issue_id,
                        "agent": context.agent,
                        "kanban_task_id": task_id,
                        "status": snapshot.status,
                    },
                )

        if snapshot.status in TERMINAL_BLOCKED_STATUSES:
            events.append(("kanban.blocked", self._event_payload(context, snapshot)))
            return ExecutionResult(
                tests_status=_string_field(snapshot.metadata, "tests_status"),
                review_status=_string_field(snapshot.metadata, "review_status"),
                events=tuple(events),
            )

        fields = _result_fields(snapshot.metadata)
        if snapshot.latest_run:
            fields = {**_result_fields(snapshot.latest_run.metadata), **fields}
        events.append(("kanban.completed", self._event_payload(context, snapshot, **fields)))
        return ExecutionResult(
            commit_sha=fields.get("commit_sha"),
            pull_request_url=fields.get("pull_request_url"),
            railway_deployment_id=fields.get("railway_deployment_id"),
            tests_status=fields.get("tests_status"),
            review_status=fields.get("review_status"),
            events=tuple(events),
        )

    @staticmethod
    def _event_payload(
        context: ExecutionContext,
        snapshot: KanbanTaskSnapshot,
        **extra: object,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "run_id": context.run_id,
            "linear_issue_id": context.linear_issue_id,
            "agent": context.agent,
            "kanban_task_id": snapshot.task_id,
            "status": snapshot.status,
        }
        if snapshot.assignee:
            payload["assignee"] = snapshot.assignee
        latest = snapshot.latest_run
        if latest and latest.outcome:
            payload["kanban_outcome"] = latest.outcome
        for key, value in extra.items():
            if value is not None:
                payload[key] = value
        return payload


def _string_field(metadata: Mapping[str, object], name: str) -> str | None:
    value = metadata.get(name)
    return value if isinstance(value, str) and value else None


def _result_fields(metadata: Mapping[str, object]) -> dict[str, str]:
    fields: dict[str, str] = {}
    for name in _ALLOWED_RESULT_FIELDS:
        value = _string_field(metadata, name)
        if value is not None:
            fields[name] = value
    return fields


__all__ = [
    "KanbanDispatchStarter",
    "KanbanDispatcherAdapter",
    "KanbanRunSnapshot",
    "KanbanTaskReader",
    "KanbanTaskSnapshot",
]
