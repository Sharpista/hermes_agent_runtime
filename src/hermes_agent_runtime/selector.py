"""Linear issue selection policy for the Hermes runtime orchestrator chain.

The selector is intentionally adapter-shaped: callers may convert real Linear SDK
objects into ``LinearIssueSnapshot`` without importing a Linear client here. This
keeps the runtime package free of third-party dependencies while versioning the
policy that decides whether an issue may reach ``AgentRuntime.execute``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .runtime import ExecutionBlocked, Issue, classify_issue


@dataclass(frozen=True)
class LinearIssueSnapshot:
    """Minimal, non-secret view of a Linear issue used by the runtime chain."""

    identifier: str
    status: str
    labels: frozenset[str]


@dataclass(frozen=True)
class SelectionDecision:
    """Result of applying the orchestrator policy to a Linear issue."""

    issue: Issue
    action: Literal["eligible", "blocked", "ignored"]
    reason: str | None = None
    assignee: str | None = None
    risk: str | None = None
    execution_mode: str | None = None
    environment: str | None = None

    @property
    def eligible(self) -> bool:
        return self.action == "eligible"


class LinearIssueSelector:
    """Apply Spec 009/T-ORCH-2 label policy before any worker is spawned."""

    def select(self, snapshot: LinearIssueSnapshot) -> SelectionDecision:
        issue = Issue(snapshot.identifier, frozenset(snapshot.labels), snapshot.status)
        if snapshot.status != "Todo":
            return SelectionDecision(issue, "ignored", "Only Todo issues are candidates")
        try:
            assignee, risk, mode, environment = classify_issue(issue)
        except ExecutionBlocked as exc:
            # ``ExecutionBlocked`` messages are static policy strings from runtime.py;
            # they never include Linear titles, descriptions, comments or secrets.
            return SelectionDecision(issue, "blocked", str(exc), None, None, None, None)
        return SelectionDecision(issue, "eligible", None, assignee, risk, mode, environment)


__all__ = ["LinearIssueSelector", "LinearIssueSnapshot", "SelectionDecision"]
