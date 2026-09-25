"""Synchronous lifecycle boundary; the installed Hermes dispatcher is injected by the caller."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import secrets
from threading import Event, Thread
from typing import Callable, Mapping, Protocol, runtime_checkable

from .payloads import validate_event_payload


ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
AGENTS = {
    "agent:orchestrator": "orchestrator",
    "agent:backend": "dev-backend",
    "agent:frontend": "dev-frontend",
    "agent:devops": "devops",
    "agent:qa": "qualidade",
    "agent:review": "code-reviewer",
    "agent:github": "github-profile",
}


def new_run_id() -> str:
    """Produce a 48-bit millisecond timestamp plus 80 random bits as a ULID."""
    millis = int(datetime.now(timezone.utc).timestamp() * 1000)
    value = (millis << 80) | secrets.randbits(80)
    return "run_" + "".join(ALPHABET[(value >> (5 * i)) & 31] for i in range(25, -1, -1))


class RunConflict(Exception):
    """Another active run already owns the issue."""


class ExecutionBlocked(Exception):
    """Issue classification or policy requires orchestrator intervention."""


class ExecutionEventError(RuntimeError):
    """Dispatch failed after observing events that must be persisted first."""

    def __init__(self, error_type: str, events: tuple[tuple[str, Mapping[str, object]], ...]):
        super().__init__(error_type)
        self.error_type = error_type
        self.events = events


@dataclass(frozen=True)
class Issue:
    identifier: str
    labels: frozenset[str]
    status: str = "Todo"


@dataclass(frozen=True)
class ExecutionContext:
    run_id: str
    linear_issue_id: str
    agent: str
    risk: str
    execution_mode: str
    environment: str


@dataclass(frozen=True)
class ExecutionResult:
    commit_sha: str | None = None
    pull_request_url: str | None = None
    railway_deployment_id: str | None = None
    tests_status: str | None = None
    review_status: str | None = None
    events: tuple[tuple[str, Mapping[str, object]], ...] = field(default_factory=tuple)

    def fields(self) -> dict[str, str]:
        return {name: value for name in (
            "commit_sha", "pull_request_url", "railway_deployment_id",
            "tests_status", "review_status"
        ) if (value := getattr(self, name)) is not None}


class Store(Protocol):
    def claim(self, context: ExecutionContext, ttl_seconds: int) -> None: ...
    def heartbeat(self, context: ExecutionContext, ttl_seconds: int) -> bool: ...
    def event(self, context: ExecutionContext, kind: str, payload: Mapping[str, object]) -> None: ...
    def finish(self, context: ExecutionContext, status: str, fields: Mapping[str, object]) -> None: ...
    def recover(self, issue_id: str) -> bool: ...


@runtime_checkable
class LockRejectionSink(Protocol):
    def lock_rejected(self, context: ExecutionContext, reason: str) -> None: ...


class AgentRuntime:
    def __init__(self, store: Store, *, ttl_seconds: int = 600, heartbeat_seconds: int = 30):
        if ttl_seconds < 10 or heartbeat_seconds < 1 or heartbeat_seconds * 2 >= ttl_seconds:
            raise ValueError("heartbeat must be less than half the lock TTL")
        self.store = store
        self.ttl_seconds = ttl_seconds
        self.heartbeat_seconds = heartbeat_seconds

    def classify(self, issue: Issue) -> tuple[str, str, str, str]:
        if issue.status != "Todo":
            raise ExecutionBlocked("Only Todo issues can start a new run")
        unknown_agent_labels = [label for label in issue.labels if label.startswith("agent:") and label not in AGENTS]
        if unknown_agent_labels:
            raise ExecutionBlocked("Unknown agent label")
        agents = issue.labels.intersection(AGENTS)
        if len(agents) != 1:
            raise ExecutionBlocked("Orchestrator must classify missing or ambiguous agent labels")
        def one(prefix: str, default: str) -> str:
            matches = [label.removeprefix(prefix) for label in issue.labels if label.startswith(prefix)]
            if len(matches) > 1:
                raise ExecutionBlocked(f"Conflicting {prefix} labels")
            return matches[0] if matches else default
        risk = one("risk:", "low")
        mode = one("execution:", "auto")
        environment = one("env:", "local")
        if risk not in {"low", "medium", "high", "critical"} or mode not in {"auto", "human", "blocked"} or environment not in {"local", "preview", "staging", "production"}:
            raise ExecutionBlocked("Unknown operational label")
        if mode != "auto" or risk == "critical" or environment == "production":
            raise ExecutionBlocked("This issue requires human intervention")
        return AGENTS[next(iter(agents))], risk, mode, environment

    def execute(self, issue: Issue, dispatch: Callable[[ExecutionContext], ExecutionResult],
                *, move_status: Callable[[str, str], None] | None = None) -> ExecutionContext:
        agent, risk, mode, environment = self.classify(issue)
        context = ExecutionContext(new_run_id(), issue.identifier, agent, risk, mode, environment)
        # One database transaction inserts the run and lock. A conflict creates no orphan run.
        try:
            self.store.claim(context, self.ttl_seconds)
        except RunConflict:
            if isinstance(self.store, LockRejectionSink):
                self.store.lock_rejected(context, "RunConflict")
            raise
        stop = Event()
        heartbeat_errors: list[BaseException] = []

        def pulse() -> None:
            while not stop.wait(self.heartbeat_seconds):
                try:
                    if not self.store.heartbeat(context, self.ttl_seconds):
                        raise RuntimeError("Lock ownership lost")
                except BaseException as exc:
                    heartbeat_errors.append(exc)
                    return

        worker = Thread(target=pulse, name=f"heartbeat-{context.run_id}", daemon=True)
        worker.start()
        try:
            if move_status:
                move_status(issue.identifier, "In Progress")
            self.store.event(context, "agent.dispatched", validate_event_payload("agent.dispatched", {}))
            result = dispatch(context)
            stop.set()
            worker.join()
            self._persist_events(context, result.events)
            if heartbeat_errors:
                raise RuntimeError("Heartbeat failed; execution outcome requires review")
            if agent in {"dev-backend", "dev-frontend", "devops"} and (
                result.tests_status != "passed" or result.review_status != "approved"
            ):
                raise ExecutionBlocked("Required test and review gates are incomplete")
            if move_status:
                move_status(issue.identifier, "In Review")
            # Dispatcher must supply QA/review results; this boundary never marks Linear Done.
            self.store.finish(context, "completed", result.fields())
            return context
        except Exception as exc:
            stop.set()
            worker.join()
            finish_error: BaseException = exc
            if isinstance(exc, ExecutionEventError):
                try:
                    self._persist_events(context, exc.events)
                except Exception as event_exc:
                    finish_error = event_exc
            # Keep error text out of operational tables: downstream exceptions can contain tokens.
            status = "blocked" if isinstance(finish_error, ExecutionBlocked) else "failed"
            error_type = exc.error_type if isinstance(exc, ExecutionEventError) and finish_error is exc else type(finish_error).__name__
            self.store.finish(context, status, {"error": error_type, "error_code": error_type})
            raise
        finally:
            stop.set()
            worker.join()

    def recover_expired_issue(self, issue_id: str) -> bool:
        """Orchestrator-only operation; the SQL function checks expiry under row lock."""
        return self.store.recover(issue_id)

    def _persist_events(self, context: ExecutionContext, events: tuple[tuple[str, Mapping[str, object]], ...]) -> None:
        seen: set[tuple[object, ...]] = set()
        normalized_events: list[tuple[str, Mapping[str, object]]] = []
        for kind, payload in events:
            normalized = validate_event_payload(kind, payload)
            key = _event_key(kind, normalized)
            if key in seen:
                continue
            seen.add(key)
            normalized_events.append((kind, normalized))
        for kind, normalized in normalized_events:
            self.store.event(context, kind, normalized)


def _event_key(kind: str, payload: Mapping[str, object]) -> tuple[object, ...]:
    task_id = payload.get("kanban_task_id")
    if task_id is not None:
        return (kind, task_id, payload.get("status"), payload.get("kanban_outcome"))
    return (kind, json.dumps(dict(payload), sort_keys=True, separators=(",", ":")))
