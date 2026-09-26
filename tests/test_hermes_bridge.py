import json
import unittest
from unittest.mock import patch

from hermes_agent_runtime.hermes_bridge import _read_task, _start, dispatch_stage
from hermes_agent_runtime.linear import LinearIssue
from hermes_agent_runtime.orchestrator import StageRequest
from hermes_agent_runtime.runtime import ExecutionBlocked, ExecutionContext


def request(stage="implement", workspace=None):
    context = ExecutionContext("run_01K5ZGQNHJDDB44RKGSZXAN7F5", "LOL-1", "dev-backend",
                               "low", "auto", "local")
    issue = LinearIssue("uuid", "LOL-1", "Fix", "Check expected behavior", frozenset(), "Todo", "team")
    return StageRequest(context, issue, stage, "a" * 40, workspace, "feat/lol-1")


class HermesBridgeTests(unittest.TestCase):
    @patch("hermes_agent_runtime.hermes_bridge.subprocess.run")
    def test_create_uses_board_project_and_idempotency_without_shell(self, run):
        run.return_value.returncode = 0
        run.return_value.stdout = '{"id":"t_123"}'
        with patch.dict("os.environ", {"HERMES_KANBAN_BOARD": "lolcoach",
                                    "HERMES_KANBAN_PROJECT": "lolcoach",
                                    "LINEAR_API_KEY": "private", "SUPABASE_SERVICE_ROLE_KEY": "private"}):
            self.assertEqual("t_123", _start(request()))
        argv = run.call_args.args[0]
        self.assertEqual(["hermes", "kanban", "--board", "lolcoach", "create"], argv[:5])
        self.assertEqual("lolcoach", argv[argv.index("--project") + 1])
        self.assertEqual("run_01K5ZGQNHJDDB44RKGSZXAN7F5:implement",
                         argv[argv.index("--idempotency-key") + 1])
        self.assertNotIn("private", str(run.call_args.kwargs["env"]))
        self.assertIn("LOL-1", run.call_args.kwargs["input"])

    @patch("hermes_agent_runtime.hermes_bridge.subprocess.run")
    def test_show_maps_latest_run_and_workspace(self, run):
        run.return_value.returncode = 0
        run.return_value.stdout = json.dumps({
            "task": {"id": "t_123", "status": "done", "assignee": "qualidade",
                     "workspace_path": "/tmp/worktree", "branch_name": "feat/lol-1"},
            "runs": [{"status": "completed", "outcome": "completed",
                      "metadata": {"commit_sha": "a" * 40, "tests_status": "passed"}}],
        })
        snapshot = _read_task("t_123")
        self.assertEqual("/tmp/worktree", snapshot.workspace_path)
        self.assertEqual("passed", snapshot.latest_run.metadata["tests_status"])

    @patch("hermes_agent_runtime.hermes_bridge.subprocess.run")
    def test_cli_error_does_not_expose_stderr(self, run):
        run.return_value.returncode = 2
        run.return_value.stderr = "secret credential"
        with patch.dict("os.environ", {"HERMES_KANBAN_PROJECT": "lolcoach"}):
            with self.assertRaisesRegex(RuntimeError, "exit 2") as raised:
                _start(request())
        self.assertNotIn("secret", str(raised.exception))

    def test_followup_requires_verified_workspace(self):
        with self.assertRaises(ExecutionBlocked):
            _start(request("quality"))

    @patch("hermes_agent_runtime.hermes_bridge._read_task")
    @patch("hermes_agent_runtime.hermes_bridge._start")
    def test_dispatch_waits_for_terminal_metadata(self, start, read):
        from hermes_agent_runtime.kanban import KanbanRunSnapshot, KanbanTaskSnapshot
        start.return_value = "t_123"
        read.return_value = KanbanTaskSnapshot(
            "t_123", "done", workspace_path="/tmp/worktree", branch="feat/lol-1",
            latest_run=KanbanRunSnapshot(outcome="completed", metadata={"commit_sha": "a" * 40}),
        )
        result = dispatch_stage(request("candidate_commit", "/tmp/worktree"))
        self.assertEqual("a" * 40, result.commit_sha)
        self.assertEqual("/tmp/worktree", result.workspace_path)


if __name__ == "__main__":
    unittest.main()
