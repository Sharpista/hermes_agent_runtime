"""Linear -> Hermes stage coordinator; each stage has its own Supabase run/lock."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

from .linear import LinearIssue
from .runtime import AGENTS, AgentRuntime, ExecutionBlocked, ExecutionContext, ExecutionResult, Issue


IMPLEMENTERS = frozenset({"dev-backend", "dev-frontend", "dev-database", "devops"})


@dataclass(frozen=True)
class StageRequest:
    context: ExecutionContext
    issue: LinearIssue
    stage: str
    candidate_sha: str | None = None


class LinearGateway(Protocol):
    def issue(self, issue_id: str) -> LinearIssue: ...
    def move(self, issue: LinearIssue, status: str) -> None: ...


class Orchestrator:
    """Run a serial handoff with a lock for every agent and SHA-bound gates.

    The injected dispatch callback must use Hermes's installed profile dispatcher;
    returning a task id alone is insufficient. Wrap it with KanbanDispatcherAdapter
    so the callback only returns after a terminal task snapshot.
    """

    def __init__(self, linear: LinearGateway, runtime: AgentRuntime,
                 dispatch: Callable[[StageRequest], ExecutionResult], *, allow_pr_publish: bool = False):
        self.linear = linear
        self.runtime = runtime
        self.dispatch = dispatch
        self.allow_pr_publish = allow_pr_publish

    def execute(self, issue_id: str) -> str:
        issue = self.linear.issue(issue_id)
        if issue.status != "Todo":
            raise ExecutionBlocked("Issue must be Todo to begin")
        self.runtime.recover_expired_issue(issue.identifier)
        agent_labels = {label for label in issue.labels if label.startswith("agent:")}
        if agent_labels - AGENTS.keys():
            raise ExecutionBlocked("Unknown agent label")
        if not agent_labels or len(agent_labels) > 1:
            # Keep all original labels for the handoff; classification is never guessed.
            agent = "orchestrator"
        else:
            agent, _, _, _ = self.runtime.classify(Issue(issue.identifier, issue.labels))
        if agent == "orchestrator":
            # Classification/decomposition is a real handoff, not a code task.
            self._stage(issue, agent, "classify", move_to="In Progress")
            return "awaiting_decomposition"
        if agent not in IMPLEMENTERS:
            self._stage(issue, agent, "specialist", move_to="In Progress")
            return "awaiting_orchestrator_review"

        self._stage(issue, agent, "implement", move_to="In Progress")
        candidate = self._stage(issue, "github-profile", "candidate_commit")
        sha = candidate.commit_sha
        if not sha or len(sha) != 40:
            raise ExecutionBlocked("Candidate commit SHA missing")
        qa = self._stage(issue, "qualidade", "quality", candidate_sha=sha)
        if qa.tests_status != "passed" or qa.commit_sha != sha:
            raise ExecutionBlocked("Quality did not approve candidate SHA")
        review = self._stage(issue, "code-reviewer", "review", candidate_sha=sha, move_to="In Review")
        if review.review_status != "approved" or review.commit_sha != sha:
            raise ExecutionBlocked("Code review did not approve candidate SHA")
        if not self.allow_pr_publish:
            raise ExecutionBlocked("PR publication needs explicit authorization")
        published = self._stage(issue, "github-profile", "publish_pr", candidate_sha=sha)
        if published.commit_sha != sha or not published.pull_request_url:
            raise ExecutionBlocked("PR missing or candidate SHA changed")
        # CI and external acceptance still need verification before Done.
        return published.pull_request_url

    def _stage(self, issue: LinearIssue, agent: str, stage: str, *,
               candidate_sha: str | None = None, move_to: str | None = None) -> ExecutionResult:
        labels = frozenset(label for label in issue.labels if not label.startswith("agent:")) | {
            next(label for label, name in AGENTS.items() if name == agent)
        }
        result: ExecutionResult | None = None

        def run(context: ExecutionContext) -> ExecutionResult:
            nonlocal result
            result = self.dispatch(StageRequest(context, issue, stage, candidate_sha))
            if not isinstance(result, ExecutionResult):
                raise TypeError("Hermes dispatcher must return ExecutionResult")
            if stage == "candidate_commit" and not _valid_sha(result.commit_sha):
                raise ExecutionBlocked("Candidate commit SHA missing")
            if stage == "quality" and (result.tests_status != "passed" or result.commit_sha != candidate_sha):
                raise ExecutionBlocked("Quality did not approve candidate SHA")
            if stage == "review" and (result.review_status != "approved" or result.commit_sha != candidate_sha):
                raise ExecutionBlocked("Code review did not approve candidate SHA")
            if stage == "publish_pr" and (result.commit_sha != candidate_sha or not result.pull_request_url):
                raise ExecutionBlocked("PR missing or candidate SHA changed")
            return result

        self.runtime.execute(Issue(issue.identifier, frozenset(labels)), run,
                             move_status=(lambda _id, state: self.linear.move(issue, state)) if move_to == "In Progress" else None,
                             require_gates=False, move_to_review=False)
        if move_to == "In Review":
            self.linear.move(issue, "In Review")
        assert result is not None
        return result


def _valid_sha(value: str | None) -> bool:
    import re
    return bool(value and re.fullmatch(r"[0-9a-fA-F]{40}", value))
