import unittest
from email.message import Message
from io import BytesIO
from unittest.mock import patch
from urllib.error import HTTPError

from hermes_agent_runtime.runtime import (
    AgentRuntime, ExecutionBlocked, ExecutionContext, ExecutionResult, Issue, RunConflict, new_run_id,
)
from hermes_agent_runtime.kanban import (
    KanbanDispatcherAdapter, KanbanRunSnapshot, KanbanTaskSnapshot,
)
from hermes_agent_runtime.supabase import SupabaseStore


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

    def lock_rejected(self, context, reason):
        self.events.append((context.run_id, "lock.rejected", reason))

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
        self.assertEqual("lock.rejected", self.store.events[-1][1])

    def test_failure_is_terminal_and_releases_lock(self):
        def broken(_):
            raise ValueError("credential must not be persisted")
        with self.assertRaises(ValueError):
            self.runtime.execute(self.issue, broken)
        self.assertEqual(["failed"], list(self.store.runs.values()))
        self.assertEqual({}, self.store.active)

    def test_missing_quality_gate_is_blocked(self):
        with self.assertRaises(ExecutionBlocked):
            self.runtime.execute(self.issue, lambda _: ExecutionResult(
                tests_status="passed",
                events=(("tests.completed", {"passed": 7}),),
            ))
        self.assertEqual(["blocked"], list(self.store.runs.values()))
        self.assertEqual({}, self.store.active)
        self.assertTrue(any(event[1] == "tests.completed" for event in self.store.events))

    def test_ambiguous_or_sensitive_issue_is_not_dispatched(self):
        for labels in (
            {"agent:backend", "agent:frontend"},
            {"agent:backend", "env:production"},
            {"agent:backend", "risk:critical"},
            {"agent:backend", "execution:human"},
            {"agent:backend", "agent:unknown"},
            set(),
        ):
            with self.subTest(labels=labels), self.assertRaises(ExecutionBlocked):
                self.runtime.execute(Issue("LOL-57", frozenset(labels)), lambda _: None)


class SupabaseStoreTests(unittest.TestCase):
    def test_claim_translates_http_409_to_run_conflict(self):
        store = SupabaseStore("https://example.supabase.co", "service-role-key")
        error = HTTPError(
            "https://example.supabase.co/rest/v1/rpc/hermes_claim_run",
            409,
            "Conflict",
            hdrs=Message(),
            fp=BytesIO(b'{"code":"23505"}'),
        )

        with patch("hermes_agent_runtime.supabase.urlopen", side_effect=error):
            with self.assertRaises(RunConflict):
                store.claim(next_context(), ttl_seconds=60)


class KanbanAdapterTests(unittest.TestCase):
    def setUp(self):
        self.context = next_context()

    def test_done_task_maps_metadata_to_execution_result(self):
        snapshot = KanbanTaskSnapshot(
            "t_backend",
            "done",
            assignee="dev-backend",
            metadata={
                "tests_status": "passed",
                "review_status": "approved",
                "commit_sha": "abc123",
                "pull_request_url": "https://github.com/org/repo/pull/1",
            },
        )
        adapter = KanbanDispatcherAdapter(lambda _: snapshot, lambda task_id: snapshot)

        result = adapter(self.context)

        self.assertEqual("passed", result.tests_status)
        self.assertEqual("approved", result.review_status)
        self.assertEqual("abc123", result.commit_sha)
        self.assertEqual("https://github.com/org/repo/pull/1", result.pull_request_url)
        self.assertEqual("kanban.dispatched", result.events[0][0])
        self.assertEqual("kanban.completed", result.events[-1][0])

    def test_spawn_is_not_success_until_task_reaches_done(self):
        snapshots = [
            KanbanTaskSnapshot("t_backend", "running", assignee="dev-backend"),
            KanbanTaskSnapshot(
                "t_backend",
                "done",
                assignee="dev-backend",
                metadata={"tests_status": "passed", "review_status": "approved"},
            ),
        ]

        def read_task(_):
            return snapshots.pop(0)

        adapter = KanbanDispatcherAdapter(
            lambda _: "t_backend",
            read_task,
            poll_seconds=0.01,
            sleep=lambda _: None,
            monotonic=fake_monotonic(),
        )

        result = adapter(self.context)

        self.assertEqual("approved", result.review_status)
        self.assertTrue(any(event[0] == "kanban.status_changed" for event in result.events))

    def test_blocked_task_returns_gate_missing_result(self):
        snapshot = KanbanTaskSnapshot("t_backend", "blocked", metadata={"tests_status": "failed"})
        adapter = KanbanDispatcherAdapter(lambda _: snapshot, lambda task_id: snapshot)

        result = adapter(self.context)

        self.assertEqual("failed", result.tests_status)
        self.assertIsNone(result.review_status)
        self.assertEqual("kanban.blocked", result.events[-1][0])

    def test_failed_worker_outcome_raises_before_success(self):
        snapshot = KanbanTaskSnapshot(
            "t_backend",
            "running",
            latest_run=KanbanRunSnapshot(status="ended", outcome="failed"),
        )
        adapter = KanbanDispatcherAdapter(lambda _: snapshot, lambda task_id: snapshot)

        with self.assertRaises(RuntimeError):
            adapter(self.context)


def next_context():
    return ExecutionContext("run_01M3A9VMVX4JFQ6CP7ZPWRKM8K", "LOL-56", "dev-backend", "medium", "auto", "local")


def fake_monotonic():
    value = {"now": 0.0}

    def tick():
        value["now"] += 0.01
        return value["now"]

    return tick


if __name__ == "__main__":
    unittest.main()
