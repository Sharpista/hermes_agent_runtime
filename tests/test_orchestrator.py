import unittest

from hermes_agent_runtime.linear import LinearIssue
from hermes_agent_runtime.orchestrator import Orchestrator
from hermes_agent_runtime.runtime import AgentRuntime, ExecutionBlocked, ExecutionResult, RunConflict


SHA = "a" * 40


class Store:
    def __init__(self):
        self.active = set()
        self.finished = []
        self.events = []

    def claim(self, context, ttl_seconds):
        if context.linear_issue_id in self.active:
            raise RunConflict()
        self.active.add(context.linear_issue_id)

    def heartbeat(self, context, ttl_seconds):
        return context.linear_issue_id in self.active

    def event(self, context, kind, payload):
        self.events.append((context.agent, kind))

    def finish(self, context, status, fields):
        self.finished.append((context.agent, status, dict(fields)))
        self.active.remove(context.linear_issue_id)

    def recover(self, issue_id):
        return False


class Linear:
    def __init__(self, labels=frozenset({"agent:backend"})):
        self.record = LinearIssue("uuid", "LOL-1", "Fix bug", "Acceptance criteria", labels, "Todo", "team")
        self.statuses = []

    def issue(self, issue_id):
        assert issue_id == "LOL-1"
        return self.record

    def move(self, issue, status):
        self.statuses.append(status)


class OrchestratorTests(unittest.TestCase):
    def setUp(self):
        self.linear = Linear()
        self.store = Store()
        self.calls = []

    def dispatch(self, request):
        self.calls.append(request)
        if request.stage == "candidate_commit":
            return ExecutionResult(commit_sha=SHA)
        if request.stage == "quality":
            return ExecutionResult(commit_sha=SHA, tests_status="passed")
        if request.stage == "review":
            return ExecutionResult(commit_sha=SHA, review_status="approved")
        if request.stage == "publish_pr":
            return ExecutionResult(commit_sha=SHA, pull_request_url="https://github.com/a/b/pull/1")
        return ExecutionResult()

    def coordinator(self, dispatch=None):
        return Orchestrator(self.linear, AgentRuntime(self.store), dispatch or self.dispatch,
                            allow_pr_publish=True)

    def test_full_chain_requires_sha_bound_gates_and_pr(self):
        result = self.coordinator().execute("LOL-1")
        self.assertEqual("https://github.com/a/b/pull/1", result)
        self.assertEqual(["implement", "candidate_commit", "quality", "review", "publish_pr"],
                         [call.stage for call in self.calls])
        self.assertEqual(["In Progress", "In Review"], self.linear.statuses)
        self.assertEqual(["completed"] * 5, [status for _, status, _ in self.store.finished])
        self.assertEqual({SHA}, {call.candidate_sha for call in self.calls[2:]})

    def test_review_on_other_sha_blocks_without_pr_or_done(self):
        def dispatch(request):
            if request.stage == "review":
                return ExecutionResult(commit_sha="b" * 40, review_status="approved")
            return self.dispatch(request)
        with self.assertRaises(ExecutionBlocked):
            self.coordinator(dispatch).execute("LOL-1")
        self.assertEqual(["In Progress"], self.linear.statuses)
        self.assertEqual("blocked", self.store.finished[-1][1])
        self.assertFalse(self.store.active)

    def test_review_rework_creates_new_sha_and_rechecks(self):
        corrected = "b" * 40
        count = 0
        def dispatch(request):
            nonlocal count
            self.calls.append(request)
            if request.stage == "candidate_commit":
                count += 1
                return ExecutionResult(commit_sha=SHA if count == 1 else corrected)
            if request.stage == "quality":
                return ExecutionResult(commit_sha=request.candidate_sha, tests_status="passed")
            if request.stage == "review":
                return ExecutionResult(commit_sha=request.candidate_sha,
                                       review_status="changes_requested" if count == 1 else "approved")
            if request.stage == "publish_pr":
                return ExecutionResult(commit_sha=request.candidate_sha,
                                       pull_request_url="https://github.com/a/b/pull/2")
            return ExecutionResult()
        self.assertEqual("https://github.com/a/b/pull/2", self.coordinator(dispatch).execute("LOL-1"))
        self.assertEqual(["implement", "candidate_commit", "quality", "review", "rework",
                          "candidate_commit", "quality", "review", "publish_pr"],
                         [call.stage for call in self.calls])
        self.assertEqual(corrected, self.calls[-1].candidate_sha)
        self.assertEqual(["In Progress", "In Review"], self.linear.statuses)

    def test_no_label_goes_to_orchestrator(self):
        self.linear = Linear(frozenset())
        self.assertEqual("awaiting_decomposition", self.coordinator().execute("LOL-1"))
        self.assertEqual(["classify"], [call.stage for call in self.calls])
        self.assertEqual("orchestrator", self.calls[0].context.agent)
        self.assertNotIn("Done", self.linear.statuses)

    def test_lock_conflict_does_not_dispatch(self):
        self.store.active.add("LOL-1")
        with self.assertRaises(RunConflict):
            self.coordinator().execute("LOL-1")
        self.assertEqual([], self.calls)
        self.assertEqual([], self.linear.statuses)

    def test_pr_needs_process_authorization(self):
        coordinator = Orchestrator(self.linear, AgentRuntime(self.store), self.dispatch)
        with self.assertRaises(ExecutionBlocked):
            coordinator.execute("LOL-1")
        self.assertEqual(["implement", "candidate_commit", "quality", "review"],
                         [call.stage for call in self.calls])
        self.assertEqual(["In Progress", "In Review"], self.linear.statuses)

    def test_unknown_agent_never_spawns(self):
        self.linear = Linear(frozenset({"agent:unknown"}))
        with self.assertRaises(ExecutionBlocked):
            self.coordinator().execute("LOL-1")
        self.assertFalse(self.calls)


if __name__ == "__main__":
    unittest.main()
