"""Read-only regression test for CR-1 (LOL-101 / card t_b36cfe7c).

Proves ``poller_tick.read_task`` exposes the ``task_runs.metadata`` quality
gates in BOTH terminal statuses (``done`` and ``blocked``), so the pinned chain
(``hermes_agent_runtime.kanban``) reports ``tests_status``/``review_status``
identically in both paths.

Isolation: builds a throwaway in-memory SQLite from the real ``SCHEMA_SQL`` and
monkeypatches only the connection factory inside this process. The shared board
DB is never opened, written, dispatched to or archived, and ``poller_tick.main``
(the mutating tick) is never called.

Run through the unit entrypoint:

    run_poller.sh /home/alexandre/.hermes/runtime/poller/test_read_task_gates.py

Exit 0 = PASS, 1 = FAIL.
"""
from __future__ import annotations

import contextlib
import json
import sqlite3
import sys
from pathlib import Path

POLLER_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(POLLER_DIR))

from hermes_cli import kanban_db as kb  # noqa: E402
import hermes_cli.kanban_db_connect as kbc  # noqa: E402
import poller_tick  # noqa: E402

from hermes_agent_runtime import ExecutionContext, KanbanDispatcherAdapter  # noqa: E402

DONE_GATES = {
    "tests_status": "passed",
    "review_status": "approved",
    "commit_sha": "deadbeef",
    "pull_request_url": "https://example.invalid/pr/1",
}
BLOCKED_GATES = {"tests_status": "failed", "review_status": "pending"}


def build_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(kb.SCHEMA_SQL)
    return conn


def seed(conn: sqlite3.Connection, task_id: str, status: str, gates: dict) -> None:
    conn.execute(
        "INSERT INTO tasks (id, title, body, assignee, status, priority, created_at, workspace_kind) "
        "VALUES (?, ?, NULL, 'dev-backend', ?, 0, 1, 'scratch')",
        (task_id, f"[runtime] {task_id}", status),
    )
    conn.execute(
        "INSERT INTO task_runs (task_id, status, started_at, ended_at, outcome, summary, metadata) "
        "VALUES (?, ?, 1, 2, ?, ?, ?)",
        (task_id, status, status, f"run for {task_id}", json.dumps(gates)),
    )
    conn.commit()


def main() -> int:
    checks: list[tuple[str, bool, str]] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        checks.append((label, bool(ok), detail))
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}: {detail}")

    conn = build_conn()
    seed(conn, "t_done", "done", DONE_GATES)
    seed(conn, "t_blocked", "blocked", BLOCKED_GATES)

    real_connect = kbc.connect_closing

    @contextlib.contextmanager
    def fake_connect(*, board=None):  # noqa: ANN001, ANN202
        yield conn

    kbc.connect_closing = fake_connect
    try:
        snap_done = poller_tick.read_task("t_done")
        snap_blocked = poller_tick.read_task("t_blocked")
    finally:
        kbc.connect_closing = real_connect

    print("read_task snapshots (in-memory task_runs.metadata -> snapshot):")
    print(f"  t_done    status={snap_done.status} metadata={dict(snap_done.metadata)}")
    print(f"  t_blocked status={snap_blocked.status} metadata={dict(snap_blocked.metadata)}")

    check(
        "done_metadata_has_gates",
        snap_done.metadata.get("tests_status") == "passed"
        and snap_done.metadata.get("review_status") == "approved",
        str(dict(snap_done.metadata)),
    )
    check(
        "blocked_metadata_has_gates",
        snap_blocked.metadata.get("tests_status") == "failed"
        and snap_blocked.metadata.get("review_status") == "pending",
        str(dict(snap_blocked.metadata)),
    )
    check(
        "done_latest_run_metadata_preserved",
        snap_done.latest_run is not None
        and snap_done.latest_run.metadata.get("commit_sha") == "deadbeef",
        str(dict(snap_done.latest_run.metadata) if snap_done.latest_run else {}),
    )

    ctx = ExecutionContext(
        run_id="run_test", linear_issue_id="LOL-TEST", agent="dev-backend",
        risk="low", execution_mode="auto", environment="local",
    )

    res_done = KanbanDispatcherAdapter(lambda _: snap_done, lambda _tid: snap_done)(ctx)
    check(
        "chain_done_maps_gates",
        res_done.tests_status == "passed" and res_done.review_status == "approved"
        and res_done.commit_sha == "deadbeef",
        f"tests={res_done.tests_status} review={res_done.review_status} commit={res_done.commit_sha}",
    )

    res_blocked = KanbanDispatcherAdapter(lambda _: snap_blocked, lambda _tid: snap_blocked)(ctx)
    check(
        "chain_blocked_maps_gates",
        res_blocked.tests_status == "failed" and res_blocked.review_status == "pending",
        f"tests={res_blocked.tests_status} review={res_blocked.review_status}",
    )
    check("chain_blocked_event", res_blocked.events[-1][0] == "kanban.blocked", res_blocked.events[-1][0])

    failed = [label for label, ok, _ in checks if not ok]
    print(f"RESULT: {'PASS' if not failed else 'FAIL'} ({len(checks) - len(failed)}/{len(checks)} checks)")
    if failed:
        print("FAILED: " + ", ".join(failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
