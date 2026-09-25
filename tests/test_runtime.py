import unittest
from email.message import Message
from io import BytesIO
from unittest.mock import patch
from urllib.error import HTTPError

from hermes_agent_runtime.payloads import PayloadValidationError, validate_event_payload
from hermes_agent_runtime.runtime import (
    AgentRuntime, ExecutionBlocked, ExecutionContext, ExecutionEventError, ExecutionResult, Issue, RunConflict, new_run_id,
)
from hermes_agent_runtime.supabase import SupabaseStore
from hermes_agent_runtime.kanban import (
    KanbanDispatcherAdapter, KanbanRunSnapshot, KanbanTaskSnapshot,
)


class MemoryStore:
    def __init__(self):
        self.active = {}
        self.runs = {}
        self.events = []
        self.calls = []
        self.finish_fields = {}

    def claim(self, context, ttl_seconds):
        if context.linear_issue_id in self.active:
            raise RunConflict()
        self.active[context.linear_issue_id] = context.run_id
        self.runs[context.run_id] = "running"
        self.events.append((context.run_id, "lock.acquired"))
        self.calls.append(("claim", context.run_id))

    def heartbeat(self, context, ttl_seconds):
        return self.active.get(context.linear_issue_id) == context.run_id

    def event(self, context, kind, payload):
        self.events.append((context.run_id, kind, dict(payload)))
        self.calls.append(("event", kind))

    def lock_rejected(self, context, reason):
        self.events.append((context.run_id, "lock.rejected", reason))

    def finish(self, context, status, fields):
        if self.active.get(context.linear_issue_id) != context.run_id:
            raise RuntimeError("wrong owner")
        self.runs[context.run_id] = status
        self.finish_fields[context.run_id] = dict(fields)
        self.calls.append(("finish", status, dict(fields)))
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
                                   events=(("tests.completed", {"tests_status": "passed", "passed": 7}),))
        context = self.runtime.execute(self.issue, dispatch,
            move_status=lambda issue, status: transitions.append((issue, status)))
        self.assertEqual(["In Progress", "In Review"], [x[1] for x in transitions])
        self.assertEqual("dev-backend", context.agent)
        self.assertEqual(context, seen[0])
        self.assertEqual("completed", self.store.runs[context.run_id])
        self.assertNotIn("LOL-56", self.store.active)
        self.assertTrue(any(event[:2] == (context.run_id, "tests.completed") for event in self.store.events))

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
                events=(("tests.completed", {"tests_status": "passed", "passed": 7}),),
            ))
        self.assertEqual(["blocked"], list(self.store.runs.values()))
        self.assertEqual({}, self.store.active)
        self.assertTrue(any(event[1] == "tests.completed" for event in self.store.events))

    def test_deduplicates_accumulated_events_before_finish(self):
        event = ("kanban.dispatched", {"kanban_task_id": "t_backend", "status": "running"})

        context = self.runtime.execute(self.issue, lambda _: ExecutionResult(
            tests_status="passed",
            review_status="approved",
            events=(
                event,
                event,
                ("kanban.status_changed", {"kanban_task_id": "t_backend", "status": "done"}),
            ),
        ))

        self.assertEqual(1, sum(1 for stored in self.store.events if stored[1] == "kanban.dispatched"))
        event_calls = [call for call in self.store.calls if call[0] == "event"]
        self.assertEqual(
            ["agent.dispatched", "kanban.dispatched", "kanban.status_changed"],
            [call[1] for call in event_calls],
        )
        self.assertLess(
            self.store.calls.index(("event", "kanban.status_changed")),
            next(index for index, call in enumerate(self.store.calls) if call[0] == "finish"),
        )
        self.assertEqual("completed", self.store.runs[context.run_id])

    def test_eventful_dispatch_error_persists_events_before_failed_finish(self):
        def timed_out(_):
            raise ExecutionEventError(
                "TimeoutError",
                (("kanban.timeout", {"kanban_task_id": "t_backend", "status": "running"}),),
            )

        with self.assertRaises(ExecutionEventError):
            self.runtime.execute(self.issue, timed_out)

        run_id = next(iter(self.store.runs))
        self.assertEqual("failed", self.store.runs[run_id])
        self.assertTrue(any(event[1] == "kanban.timeout" for event in self.store.events))
        self.assertEqual("TimeoutError", self.store.finish_fields[run_id]["error"])
        self.assertEqual("TimeoutError", self.store.finish_fields[run_id]["error_code"])
        self.assertLess(
            self.store.calls.index(("event", "kanban.timeout")),
            next(index for index, call in enumerate(self.store.calls) if call[0] == "finish"),
        )

    def test_eventful_payload_validation_failure_finishes_failed_with_sanitized_error(self):
        def invalid_payload(_):
            raise ExecutionEventError(
                "RuntimeError",
                (("kanban.completed", {"kanban_task_id": "t_backend", "status": "done", "stdout": "raw"}),),
            )

        with self.assertRaises(ExecutionEventError):
            self.runtime.execute(self.issue, invalid_payload)

        run_id = next(iter(self.store.runs))
        self.assertEqual("failed", self.store.runs[run_id])
        self.assertEqual("PayloadValidationError", self.store.finish_fields[run_id]["error"])
        self.assertFalse(any(event[1] == "kanban.completed" for event in self.store.events))

    def test_completed_kanban_event_survives_quality_gate_block(self):
        snapshot = KanbanTaskSnapshot(
            "t_backend",
            "done",
            assignee="dev-backend",
            metadata={"tests_status": "passed"},
        )
        adapter = KanbanDispatcherAdapter(lambda _: snapshot, lambda task_id: snapshot)

        with self.assertRaises(ExecutionBlocked):
            self.runtime.execute(self.issue, adapter)

        run_id = next(iter(self.store.runs))
        self.assertEqual("blocked", self.store.runs[run_id])
        self.assertTrue(any(event[1] == "kanban.completed" for event in self.store.events))
        self.assertLess(
            self.store.calls.index(("event", "kanban.completed")),
            next(index for index, call in enumerate(self.store.calls) if call[0] == "finish"),
        )

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

        with self.assertRaises(ExecutionEventError) as raised:
            adapter(self.context)
        self.assertEqual("RuntimeError", raised.exception.error_type)
        self.assertEqual("kanban.dispatched", raised.exception.events[0][0])
        self.assertEqual("kanban.failed", raised.exception.events[-1][0])

    def test_timeout_raises_eventful_timeout_error(self):
        snapshot = KanbanTaskSnapshot("t_backend", "running", assignee="dev-backend")
        adapter = KanbanDispatcherAdapter(
            lambda _: snapshot,
            lambda task_id: snapshot,
            poll_seconds=0.01,
            timeout_seconds=0.01,
            sleep=lambda _: None,
            monotonic=fake_monotonic(),
        )

        with self.assertRaises(ExecutionEventError) as raised:
            adapter(self.context)
        self.assertEqual("TimeoutError", raised.exception.error_type)
        self.assertEqual("kanban.timeout", raised.exception.events[-1][0])


class PayloadContractTests(unittest.TestCase):
    def test_normalizes_empty_payloads_to_json_object(self):
        self.assertEqual({}, validate_event_payload("run.created", None))

    def test_requires_mandatory_fields(self):
        with self.assertRaises(PayloadValidationError):
            validate_event_payload("tests.completed", {"passed": 7})

    def test_rejects_unknown_or_sensitive_fields(self):
        with self.assertRaises(PayloadValidationError):
            validate_event_payload("kanban.completed", {
                "kanban_task_id": "t_backend",
                "status": "done",
                "stdout": "raw logs",
            })
        with self.assertRaises(PayloadValidationError):
            validate_event_payload("run.failed", {"error": "SUPABASE_SERVICE_ROLE_KEY leaked"})

    def test_validates_allowed_runtime_payloads(self):
        self.assertEqual(
            {"ttl_seconds": 600, "expires_at": "2026-09-25T03:00:00Z"},
            validate_event_payload("lock.acquired", {"ttl_seconds": 600, "expires_at": "2026-09-25T03:00:00Z"}),
        )
        self.assertEqual(
            {"reason": "RunConflict", "attempted_run_id": "run_01M3A9VMVX4JFQ6CP7ZPWRKM8K"},
            validate_event_payload("lock.rejected", {
                "reason": "RunConflict",
                "attempted_run_id": "run_01M3A9VMVX4JFQ6CP7ZPWRKM8K",
            }),
        )


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

    def test_claim_false_maps_to_run_conflict(self):
        store = FakeSupabaseStore([False])

        with self.assertRaises(RunConflict):
            store.claim(next_context(), 600)

    def test_event_validates_payload_before_posting(self):
        store = FakeSupabaseStore([])

        with self.assertRaises(PayloadValidationError):
            store.event(next_context(), "run.completed", {"raw_stdout": "do not persist"})

        self.assertEqual([], store.posts)

    def test_event_posts_normalized_payload_object(self):
        store = FakeSupabaseStore([None])

        store.event(next_context(), "run.created", {})

        self.assertEqual("agent_events", store.posts[0][0])
        self.assertEqual({}, store.posts[0][1]["payload"])


class FakeSupabaseStore(SupabaseStore):
    def __init__(self, responses):
        super().__init__("https://example.supabase.co", "service-role-key")
        self._responses = list(responses)
        self.posts = []

    def _post(self, path, body):
        self.posts.append((path, body))
        return self._responses.pop(0) if self._responses else None


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
