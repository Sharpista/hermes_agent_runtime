"""Polling boundary for eligible Linear issues.

The real Linear client remains outside this package. ``LinearIssuePoller`` only
coordinates a read callback, the versioned selector policy and a block recorder
so invalid issues are recorded without spawning Hermes workers.
"""

from __future__ import annotations

from typing import Iterable, Protocol

from .selector import LinearIssueSelector, LinearIssueSnapshot, SelectionDecision


class LinearIssueReader(Protocol):
    def __call__(self) -> Iterable[LinearIssueSnapshot]: ...


class BlockedIssueRecorder(Protocol):
    def __call__(self, issue_id: str, reason: str) -> None: ...


class LinearIssuePoller:
    """Read Linear candidates and return only policy-eligible issues."""

    def __init__(
        self,
        read_issues: LinearIssueReader,
        *,
        selector: LinearIssueSelector | None = None,
        record_blocked: BlockedIssueRecorder | None = None,
    ) -> None:
        self._read_issues = read_issues
        self._selector = selector or LinearIssueSelector()
        self._record_blocked = record_blocked

    def poll(self) -> tuple[SelectionDecision, ...]:
        decisions: list[SelectionDecision] = []
        for snapshot in self._read_issues():
            decision = self._selector.select(snapshot)
            if decision.action == "blocked" and self._record_blocked:
                self._record_blocked(snapshot.identifier, decision.reason or "ExecutionBlocked")
            if decision.eligible:
                decisions.append(decision)
        return tuple(decisions)


__all__ = ["BlockedIssueRecorder", "LinearIssuePoller", "LinearIssueReader"]
