"""Production-shaped single-pass poller entrypoint (LOL-97 / t_a4d0e749).

One tick: read eligible Linear issues -> versioned selector policy -> guarded
dedup/policy/bound (LOL-102 correction / t_50a168bb) -> AgentRuntime
claim/heartbeat/recovery -> real Kanban dispatch -> gates -> finish.

The tick is bounded and idempotent: ``guarded_read_issues`` skips an issue that
already has a non-archived board card, refuses ``risk:high``/``execution:human``/
``env:production``/missing-gate issues, and dispatches at most
``POLLER_MAX_DISPATCH_PER_TICK`` (default 1) per tick. ``start`` re-checks the board
by ``linear_issue_id`` before ``create_task`` and uses a linear-scoped
``idempotency_key`` as the DB-level backstop. See ``poller_dispatch_guard.py``.

It is the ExecStart target of the permanent unit ``hermes-runtime-poller.service``
and is PREPARED, not started: the card that prepared this unit (LOL-97 / t_a4d0e749)
explicitly forbids starting production. Because a tick mutates the board
(``kanban_db.create_task``) it MUST run outside a confined Hermes worker shell --
i.e. through the ``run_poller.sh`` entrypoint launched from the systemd unit.

No third-party dependency: Linear/Supabase access is plain stdlib HTTP, secrets
come from ``poller_env`` (never printed).
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

POLLER_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(POLLER_DIR))

import poller_dispatch_guard as guard  # noqa: E402
import poller_env  # noqa: E402
from smoke_lib import (  # noqa: E402  (validated clients from LOL-80)
    Linear, STATE_IN_PROGRESS, STATE_IN_REVIEW, now_iso,
)

BOARD = os.environ.get("HERMES_KANBAN_BOARD", "lolcoach")
TEAM_KEY = os.environ.get("POLLER_LINEAR_TEAM", "LOL")
POLL_SECONDS = float(os.environ.get("POLLER_POLL_SECONDS", "5"))
RUN_TIMEOUT_SECONDS = float(os.environ.get("POLLER_RUN_TIMEOUT_SECONDS", "3600"))
CLAIM_TTL_SECONDS = int(os.environ.get("POLLER_CLAIM_TTL_SECONDS", "600"))
HEARTBEAT_SECONDS = int(os.environ.get("POLLER_HEARTBEAT_SECONDS", "30"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("runtime-poller")

MAX_DISPATCH_PER_TICK = guard.DEFAULT_MAX_DISPATCH_PER_TICK

TODO_ISSUES_QUERY = """
query($team: String!) {
  issues(filter: { team: { key: { eq: $team } }, state: { name: { eq: "Todo" } } }, first: 50) {
    nodes { id identifier title state { name } labels { nodes { name } } }
  }
}
"""


def read_issues():
    """Real Linear read: only Todo candidates; the selector applies label policy."""
    from hermes_agent_runtime import LinearIssueSnapshot
    data = lin.gql(TODO_ISSUES_QUERY, {"team": TEAM_KEY})[1]
    nodes = (data or {}).get("issues", {}).get("nodes", [])
    return tuple(
        LinearIssueSnapshot(
            identifier=n["identifier"],
            status=n["state"]["name"],
            labels=frozenset(label["name"] for label in n["labels"]["nodes"]),
        )
        for n in nodes
    )


def record_blocked(issue_id: str, reason: str) -> None:
    log.warning("BLOCKED %s: %s", issue_id, reason)


def _resolve_bound() -> int:
    """Per-tick dispatch bound (``POLLER_MAX_DISPATCH_PER_TICK``, default 1)."""
    try:
        return guard.max_dispatch_per_tick()
    except ValueError as exc:
        log.warning(
            "invalid %s (%s); falling back to default %d",
            guard.BOUND_ENV_VAR, exc, guard.DEFAULT_MAX_DISPATCH_PER_TICK,
        )
        return guard.DEFAULT_MAX_DISPATCH_PER_TICK


def existing_cards(identifiers) -> dict[str, str]:
    """Read-only board scan: ``identifier -> existing non-archived card id``.

    Uses the same ``list_tasks`` the dispatcher uses and never writes: archived
    cards are ignored so archiving is the explicit way to re-allow a dispatch.
    """
    from hermes_cli.kanban_db import list_tasks
    from hermes_cli.kanban_db_connect import connect_closing

    with connect_closing(board=BOARD) as conn:
        tasks = list_tasks(conn)
    return guard.match_existing_cards(tasks, identifiers)


def guarded_read_issues():
    """The real Linear read, filtered by dedup + policy + per-tick bound.

    Wraps :func:`read_issues` so the production tick never dispatches a duplicate
    card, a sensitive issue (``risk:high``/``execution:human``/``env:production``),
    an issue missing its gate labels, or more than the configured bound per tick.
    Every skip is logged with a static reason and no secret.
    """
    snapshots = read_issues()
    if not snapshots:
        return ()
    existing = existing_cards([s.identifier for s in snapshots])
    bound = _resolve_bound()
    outcome = guard.select_with_guard(
        snapshots, existing_ids=set(existing), max_dispatch=bound,
    )
    for skip in outcome.skipped:
        detail = f"existing_card={existing[skip.identifier]}" if skip.identifier in existing else ""
        log.info("tick skip %s: %s %s", skip.identifier, skip.reason, detail)
    log.info(
        "tick guard: read=%d dispatched=%d skipped=%d max_per_tick=%d",
        len(snapshots), len(outcome.dispatched), len(outcome.skipped), bound,
    )
    return outcome.dispatched


def start(context):
    """Hand work to the installed Hermes dispatcher: create a real board task.

    Defense in depth: the tick already dedups/bounds before reaching here, but the
    board is re-checked by ``linear_issue_id`` right before ``create_task`` (the
    race window), ``idempotency_key`` is linear-scoped so a concurrent tick cannot
    duplicate the card, and a context that violates the dispatch policy is blocked
    inside the runtime (``ExecutionBlocked`` -> ``run.blocked``) rather than spawned.
    """
    from hermes_agent_runtime.runtime import ExecutionBlocked
    from hermes_cli.kanban_db import create_task, list_tasks
    from hermes_cli.kanban_db_connect import connect_closing

    if (
        context.execution_mode != "auto"
        or context.risk in {"high", "critical"}
        or context.environment == "production"
    ):
        raise ExecutionBlocked("dispatch guard: issue requires human intervention")

    with connect_closing(board=BOARD) as conn:
        existing = guard.match_existing_cards(
            list_tasks(conn), [context.linear_issue_id],
        ).get(context.linear_issue_id)
        if existing:
            log.warning(
                "start: skip %s -- existing card %s (no duplicate created)",
                context.linear_issue_id, existing,
            )
            return existing

    title = f"[runtime] {context.linear_issue_id}"
    body = (
        f"Runtime dispatch for Linear {context.linear_issue_id} (agent {context.agent}).\n\n"
        "Executar a implementacao descrita na issue Linear e concluir este card com evidencia.\n"
    )
    with connect_closing(board=BOARD) as conn:
        task_id = create_task(
            conn,
            title=title,
            body=body,
            assignee=context.agent,
            created_by="runtime-poller",
            workspace_kind="scratch",
            priority=20,
            idempotency_key=f"runtime:linear:{context.linear_issue_id}",
        )
    log.info("start: created kanban task %s assignee=%s run=%s", task_id, context.agent, context.run_id)
    return task_id


def read_task(task_id):
    """Read a task + its latest run as the gate snapshot the chain consumes.

    The quality gates (``tests_status``/``review_status``) live only in
    ``task_runs.metadata``: the ``tasks`` table has no ``metadata`` column.
    The pinned chain (``hermes_agent_runtime/kanban.py``) reads
    ``snapshot.metadata`` for a terminal ``blocked`` task and
    ``snapshot.metadata`` ∪ ``snapshot.latest_run.metadata`` for a terminal
    ``done`` task, so the run metadata must be exposed at the task level too.
    Returning ``metadata={}`` here left a blocked task with empty gates (CR-1,
    LOL-101) -- the two terminal paths therefore see the same gate source.
    """
    from hermes_agent_runtime import KanbanRunSnapshot, KanbanTaskSnapshot
    from hermes_cli.kanban_db import get_task, latest_run
    from hermes_cli.kanban_db_connect import connect_closing

    with connect_closing(board=BOARD) as conn:
        t = get_task(conn, task_id)
        if t is None:
            return KanbanTaskSnapshot(task_id=task_id, status="missing")
        r = latest_run(conn, task_id)
        run_metadata = dict(r.metadata or {}) if r else {}
        return KanbanTaskSnapshot(
            task_id=task_id,
            status=t.status,
            assignee=t.assignee,
            workspace_path=t.workspace_path,
            branch=t.branch_name,
            result=t.result,
            metadata=run_metadata,
            latest_run=KanbanRunSnapshot(
                status=r.status if r else None,
                outcome=r.outcome if r else None,
                summary=r.summary if r else None,
                metadata=run_metadata,
            ),
        )


def main() -> int:
    missing = poller_env.missing_required()
    if missing:
        log.error("missing required secrets in %s: %s", poller_env.env_file_path(), missing)
        return 78  # EX_CONFIG
    poller_env.export_into_environ()

    global lin, issue
    from hermes_agent_runtime import (
        AgentRuntime, LinearIssuePoller, LinearIssueSelector,
        RuntimeOrchestrator, SupabaseStore, kanban_dispatch_adapter,
    )
    lin = Linear()

    # map identifier -> Linear issue id so move_status can update the right issue
    issues_by_id: dict[str, dict] = {}
    raw = lin.gql(TODO_ISSUES_QUERY, {"team": TEAM_KEY})[1]
    for n in (raw or {}).get("issues", {}).get("nodes", []):
        issues_by_id[n["identifier"]] = n

    def move(identifier: str, status: str) -> None:
        target = issues_by_id.get(identifier)
        if target is None:
            log.warning("move_status: unknown issue %s (ignored)", identifier)
            return
        state_id = {"In Progress": STATE_IN_PROGRESS, "In Review": STATE_IN_REVIEW}.get(status)
        if state_id is None:
            log.warning("move_status: unmapped %r for %s (ignored)", status, identifier)
            return
        log.info("move_status %s -> %s", identifier, status)
        lin.set_state(target["id"], state_id)

    runtime = AgentRuntime(
        SupabaseStore.from_environment(), ttl_seconds=CLAIM_TTL_SECONDS, heartbeat_seconds=HEARTBEAT_SECONDS,
    )
    poller = LinearIssuePoller(guarded_read_issues, selector=LinearIssueSelector(), record_blocked=record_blocked)
    adapter = kanban_dispatch_adapter(
        start, read_task, poll_seconds=POLL_SECONDS, timeout_seconds=RUN_TIMEOUT_SECONDS,
    )
    orchestrator = RuntimeOrchestrator(
        runtime, poller, adapter, move_status=move, record_blocked=record_blocked, log=log,
    )

    log.info("tick start board=%s team=%s max_per_tick=%d", BOARD, TEAM_KEY, _resolve_bound())
    try:
        results = orchestrator.run_once()
    except Exception as exc:  # noqa: BLE001
        log.error("chain error: %s: %s", type(exc).__name__, exc)
        return 1
    for r in results:
        log.info("result issue=%s status=%s run_id=%s reason=%s", r.issue_id, r.status, r.run_id, r.reason)
    log.info("tick done at %s results=%d", now_iso(), len(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
