"""Versioned Linear -> AgentRuntime -> Hermes dispatch chain.

This module composes the poller/selector, runtime claim/recovery and dispatch
adapter. It does not import Linear, GitHub, Railway or the Hermes CLI; installed
orchestrators inject those effects through callbacks so tests can prove no
invalid issue spawns work.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Protocol

from .adapter import RuntimeDispatchAdapter, StatusUpdater
from .poller import LinearIssuePoller
from .runtime import AgentRuntime, ExecutionBlocked, RunConflict

logger = logging.getLogger(__name__)


class OrchestratorBlockRecorder(Protocol):
    def __call__(self, issue_id: str, reason: str) -> None: ...


@dataclass(frozen=True)
class OrchestratorRunResult:
    issue_id: str
    status: str
    run_id: str | None = None
    reason: str | None = None


class RuntimeOrchestrator:
    """Run one safe polling/dispatch pass for eligible Linear issues."""

    def __init__(
        self,
        runtime: AgentRuntime,
        poller: LinearIssuePoller,
        dispatch: RuntimeDispatchAdapter,
        *,
        move_status: StatusUpdater | None = None,
        record_blocked: OrchestratorBlockRecorder | None = None,
        log: logging.Logger | None = None,
    ) -> None:
        self._runtime = runtime
        self._poller = poller
        self._dispatch = dispatch
        self._move_status = move_status
        self._record_blocked = record_blocked
        self._log = log or logger

    def run_once(self) -> tuple[OrchestratorRunResult, ...]:
        results: list[OrchestratorRunResult] = []
        for decision in self._poller.poll():
            issue = decision.issue
            self._runtime.recover_expired_issue(issue.identifier)
            try:
                context = self._runtime.execute(issue, self._dispatch, move_status=self._move_status)
            except RunConflict:
                self._record(issue.identifier, "RunConflict")
                results.append(OrchestratorRunResult(issue.identifier, "conflict", reason="RunConflict"))
            except ExecutionBlocked as exc:
                reason = str(exc) or type(exc).__name__
                self._record(issue.identifier, reason)
                results.append(OrchestratorRunResult(issue.identifier, "blocked", reason=reason))
            else:
                self._log.info(
                    "runtime issue completed",
                    extra={
                        "run_id": context.run_id,
                        "linear_issue_id": context.linear_issue_id,
                        "agent": context.agent,
                    },
                )
                results.append(OrchestratorRunResult(issue.identifier, "completed", run_id=context.run_id))
        return tuple(results)

    def _record(self, issue_id: str, reason: str) -> None:
        if self._record_blocked:
            self._record_blocked(issue_id, reason)


__all__ = ["OrchestratorBlockRecorder", "OrchestratorRunResult", "RuntimeOrchestrator"]
