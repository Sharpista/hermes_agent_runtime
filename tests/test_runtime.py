import unittest

from hermes_agent_runtime.runtime import (
    AgentRuntime, ExecutionBlocked, ExecutionResult, Issue, RunConflict, new_run_id,
)


class MemoryStore:
    def __init__(self):
        self.active = {}
        self.runs = {}
        self.events = []

    def claim(self, context, ttl_seconds):
        if context.linear_issue_id in self.active:
            raise RunConflict()
        self.active[context.linear_issue_id] = context.run_id
        self.runs[context.run_id] = "running"
        self.events.append((context.run_id, "lock.acquired"))

    def heartbeat(self, context, ttl_seconds):
        return self.active.get(context.linear_issue_id) == context.run_id

    def event(self, context, kind, payload):
        self.events.append((context.run_id, kind))

    def finish(self, context, status, fields):
        if self.active.get(context.linear_issue_id) != context.run_id:
            raise RuntimeError("wrong owner")
        self.runs[context.run_id] = status
        del self.active[context.linear_issue_id]

    def recover(self, issue_id):
        return False


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.runtime = AgentRuntime(self.store)
        self.issue = Issue("LOL-56", frozenset({"agent:backend", "execution:auto", "risk:medium"}))

    def test_ulid_format(self):
        self.assertRegex(new_run_id(), r"^run_[0-9A-HJKMNP-TV-Z]{26}$")

    def test_success_reuses_id_and_finishes(self):
        transitions = []
        seen = []
        def dispatch(context):
            seen.append(context)
            return ExecutionResult(tests_status="passed", review_status="approved",
                                   events=(("tests.completed", {"passed": 7}),))
        context = self.runtime.execute(self.issue, dispatch,
            move_status=lambda issue, status: transitions.append((issue, status)))
        self.assertEqual(["In Progress", "In Review"], [x[1] for x in transitions])
        self.assertEqual("dev-backend", context.agent)
        self.assertEqual(context, seen[0])
        self.assertEqual("completed", self.store.runs[context.run_id])
        self.assertNotIn("LOL-56", self.store.active)
        self.assertIn((context.run_id, "tests.completed"), self.store.events)

    def test_concurrent_claim_does_not_dispatch_second_run(self):
        self.store.active["LOL-56"] = "run_another"
        called = []
        with self.assertRaises(RunConflict):
            self.runtime.execute(self.issue, lambda ctx: called.append(ctx))
        self.assertEqual([], called)
        self.assertEqual("run_another", self.store.active["LOL-56"])

    def test_failure_is_terminal_and_releases_lock(self):
        def broken(_):
            raise ValueError("credential must not be persisted")
        with self.assertRaises(ValueError):
            self.runtime.execute(self.issue, broken)
        self.assertEqual(["failed"], list(self.store.runs.values()))
        self.assertEqual({}, self.store.active)

    def test_missing_quality_gate_is_blocked(self):
        with self.assertRaises(ExecutionBlocked):
            self.runtime.execute(self.issue, lambda _: ExecutionResult(tests_status="passed"))
        self.assertEqual(["blocked"], list(self.store.runs.values()))
        self.assertEqual({}, self.store.active)

    def test_ambiguous_or_sensitive_issue_is_not_dispatched(self):
        for labels in (
            {"agent:backend", "agent:frontend"},
            {"agent:backend", "env:production"},
            {"agent:backend", "risk:critical"},
            {"agent:backend", "execution:human"},
            set(),
        ):
            with self.subTest(labels=labels), self.assertRaises(ExecutionBlocked):
                self.runtime.execute(Issue("LOL-57", frozenset(labels)), lambda _: None)


if __name__ == "__main__":
    unittest.main()
