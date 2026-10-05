"""Read-only guard tests for the bounded/idempotent poller tick (LOL-102 / t_50a168bb).

Covers the LOL-102 correction: dedup by ``linear_issue_id`` before creating a card,
policy refusal of ``risk:high``/``execution:human``/``env:production``/missing gates,
the configurable per-tick bound, and the contract with the pinned chain + the
installed Kanban API. Also executes the real ``poller_tick.start()`` (LOL-103
correction / t_3374c665) so the BUG-01 ImportError cannot pass unnoticed: existing
card -> skip, sensitive context -> ``ExecutionBlocked``, eligible issue -> real
``create_task`` with ``idempotency_key=runtime:linear:<ISSUE>`` on an in-memory DB.

Isolation: every check uses fakes or a throwaway IN-MEMORY SQLite built from the real
``SCHEMA_SQL``; the connection factory is monkeypatched only inside this process. The
shared board DB is never opened, created, dispatched to or archived, and the mutating
``poller_tick.main`` (a real tick) is never called.

Run through the unit entrypoint:

    run_poller.sh --guard-test

Exit 0 = PASS, 1 = FAIL.
"""
from __future__ import annotations

import contextlib
import os
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

POLLER_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(POLLER_DIR))

from hermes_cli import kanban_db as kb  # noqa: E402
import hermes_cli.kanban_db_connect as kbc  # noqa: E402
import poller_dispatch_guard as guard  # noqa: E402
import poller_tick  # noqa: E402
from hermes_agent_runtime import LinearIssueSnapshot  # noqa: E402
from hermes_agent_runtime.runtime import AGENTS as CHAIN_AGENTS, ExecutionBlocked  # noqa: E402


@dataclass(frozen=True)
class FakeTask:
    id: str
    title: str | None = None
    body: str | None = None
    status: str = "blocked"


@dataclass(frozen=True)
class StartContext:
    """Minimal ExecutionContext double for exercising the real ``poller_tick.start``."""

    linear_issue_id: str
    agent: str = "code-reviewer"
    risk: str = "low"
    execution_mode: str = "auto"
    environment: str = "local"
    run_id: str = "test-run"


def snap(identifier: str, *labels: str, status: str = "Todo") -> LinearIssueSnapshot:
    return LinearIssueSnapshot(identifier=identifier, status=status, labels=frozenset(labels))


ELIGIBLE = ("agent:review", "risk:medium", "execution:auto", "env:local")


def build_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(kb.SCHEMA_SQL)
    # Mirror the additive-column migration connect() runs; wrapped because the
    # legacy backfill opens a write txn, which a confined worker shell refuses.
    # In-fence only the base schema is needed (create_task runs out-of-fence).
    try:
        kbc._migrate_add_optional_columns(conn)
    except PermissionError:
        pass
    return conn


def main() -> int:  # noqa: C901 - explicit checks are the point of this harness
    checks: list[tuple[str, bool, str]] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        checks.append((label, bool(ok), detail))
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}: {detail}")

    print("== policy: never dispatch risk:high / execution:human / env:production / missing gates")
    check("eligible_is_none", guard.gate_block_reason("LOL-1", ELIGIBLE) is None)
    check("risk_high_blocked", guard.gate_block_reason("LOL-1", ("agent:review", "risk:high", "execution:auto", "env:local")) == "risco high exige humano")
    check("risk_critical_blocked", guard.gate_block_reason("LOL-1", ("agent:review", "risk:critical", "execution:auto", "env:local")) == "risco critical exige humano")
    check("execution_human_blocked", guard.gate_block_reason("LOL-1", ("agent:review", "risk:medium", "execution:human", "env:local")) == "execution:human exige humano")
    check("execution_blocked_blocked", (guard.gate_block_reason("LOL-1", ("agent:review", "risk:medium", "execution:blocked", "env:local")) or "").startswith("execution:blocked"))
    check("env_production_blocked", guard.gate_block_reason("LOL-1", ("agent:review", "risk:medium", "execution:auto", "env:production")) == "env:production exige humano")
    check("missing_agent_gate", (guard.gate_block_reason("LOL-1", ("risk:medium", "execution:auto", "env:local")) or "").startswith("gate ausente: exatamente um rotulo agent"))
    check("missing_risk_gate", guard.gate_block_reason("LOL-1", ("agent:review", "execution:auto", "env:local")) == "gate ausente: rotulo risk:* obrigatorio")
    check("missing_execution_gate", guard.gate_block_reason("LOL-1", ("agent:review", "risk:medium", "env:local")) == "gate ausente: rotulo execution:* obrigatorio")
    check("missing_env_gate", guard.gate_block_reason("LOL-1", ("agent:review", "risk:medium", "execution:auto")) == "gate ausente: rotulo env:* obrigatorio")
    check("ambiguous_agent_gate", (guard.gate_block_reason("LOL-1", ("agent:review", "agent:qa", "risk:medium", "execution:auto", "env:local")) or "").startswith("gate ausente: exatamente um"))
    check("unknown_agent_gate", (guard.gate_block_reason("LOL-1", ("agent:unknown", "risk:medium", "execution:auto", "env:local")) or "").startswith("gate invalido"))

    print("== bound: configurable POLLER_MAX_DISPATCH_PER_TICK")
    check("bound_default", guard.max_dispatch_per_tick({}) == guard.DEFAULT_MAX_DISPATCH_PER_TICK == 1)
    check("bound_env_override", guard.max_dispatch_per_tick({"POLLER_MAX_DISPATCH_PER_TICK": "3"}) == 3)
    try:
        guard.max_dispatch_per_tick({"POLLER_MAX_DISPATCH_PER_TICK": "nope"})
        check("bound_invalid_raises", False, "no ValueError")
    except ValueError:
        check("bound_invalid_raises", True, "ValueError")
    try:
        guard.max_dispatch_per_tick({"POLLER_MAX_DISPATCH_PER_TICK": "0"})
        check("bound_zero_raises", False, "no ValueError")
    except ValueError:
        check("bound_zero_raises", True, "ValueError")

    print("== dedup: recognize existing cards by linear_issue_id (word boundary)")
    tasks = [
        FakeTask("t_32789bf7", "[runtime] LOL-90", "Runtime dispatch for Linear LOL-90", "blocked"),
        FakeTask("t_staged", "Spec 009 staged card", "consumes LOL-76 and LOL-9 too", "blocked"),
        FakeTask("t_archived", "[runtime] LOL-63", "Runtime dispatch for Linear LOL-63", "archived"),
        FakeTask("t_hundred", "[runtime] LOL-900", "Runtime dispatch for Linear LOL-900", "blocked"),
    ]
    mapping = guard.match_existing_cards(tasks, ["LOL-90", "LOL-76", "LOL-63", "LOL-9"])
    check("dedup_title_match", mapping.get("LOL-90") == "t_32789bf7", str(mapping.get("LOL-90")))
    check("dedup_body_match_staged", mapping.get("LOL-76") == "t_staged", str(mapping.get("LOL-76")))
    check("dedup_body_match_short_issue", mapping.get("LOL-9") == "t_staged", str(mapping.get("LOL-9")))
    check("dedup_archived_ignored", "LOL-63" not in mapping, str(mapping))
    check("dedup_no_prefix_collision", "LOL-9" not in guard.match_existing_cards([tasks[0]], ["LOL-9"]))
    check("dedup_no_suffix_collision", "LOL-9" not in guard.match_existing_cards([tasks[3]], ["LOL-9"]))

    print("== select_with_guard: dedup + policy + bound keep order, skip with reason")
    candidates = [
        snap("LOL-90", *ELIGIBLE),                                        # existing card -> skip
        snap("LOL-82", "agent:devops", "risk:high", "execution:auto", "env:local"),  # policy -> skip
        snap("LOL-91", *ELIGIBLE),                                        # eligible
        snap("LOL-92", *ELIGIBLE),                                        # eligible
        snap("LOL-93", *ELIGIBLE),                                        # eligible
    ]
    out = guard.select_with_guard(candidates, existing_ids={"LOL-90"}, max_dispatch=2)
    dispatched_ids = [s.identifier for s in out.dispatched]
    reasons = {s.identifier: s.reason for s in out.skipped}
    check("bound_dispatch_count", dispatched_ids == ["LOL-91", "LOL-92"], str(dispatched_ids))
    check("dedup_skip_reason", reasons.get("LOL-90") == "card existente no board", str(reasons.get("LOL-90")))
    check("policy_skip_reason", reasons.get("LOL-82") == "risco high exige humano", str(reasons.get("LOL-82")))
    check("bound_skip_reason", reasons.get("LOL-93") == "bound do tick (max=2)", str(reasons.get("LOL-93")))

    print("== contract: policy agent set == pinned chain AGENTS")
    check("agents_contract", guard.KNOWN_AGENTS == frozenset(CHAIN_AGENTS), f"{sorted(guard.KNOWN_AGENTS)}")

    print("== contract: installed create_task is idempotent on runtime:linear:<issue>")
    skips: list[str] = []
    conn = build_conn()
    if os.environ.get("HERMES_DELEGATED_CHILD_CONTEXT"):
        skips.append("create_task idempotency contract")
        print("  [SKIP] create_task idempotency contract: confined worker shell -> run --guard-test out-of-fence")
    else:
        args = dict(
            title="[runtime] LOL-90", body="Runtime dispatch for Linear LOL-90 (agent review).",
            assignee="code-reviewer", created_by="runtime-poller", workspace_kind="scratch",
            priority=20, idempotency_key="runtime:linear:LOL-90",
        )
        first = kb.create_task(conn, **args)
        second = kb.create_task(conn, **args)
        total = conn.execute("SELECT count(*) FROM tasks WHERE idempotency_key='runtime:linear:LOL-90'").fetchone()[0]
        check("idempotency_same_id", first == second, f"{first} == {second}")
        check("idempotency_single_row", total == 1, f"rows={total}")

    print("== integration: guarded_read_issues against an in-memory board (no shared state)")
    conn.execute(
        "INSERT INTO tasks (id, title, body, assignee, status, priority, created_at, workspace_kind) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("t_prestaged_lol90", "[runtime] LOL-90",
         "Runtime dispatch for Linear LOL-90 (agent review).", "code-reviewer", "blocked", 20, 1, "scratch"),
    )
    conn.commit()
    real_connect = kbc.connect_closing

    @contextlib.contextmanager
    def fake_connect(*, board=None):  # noqa: ANN001, ANN202
        yield conn

    real_read = poller_tick.read_issues
    kbc.connect_closing = fake_connect
    os.environ["POLLER_MAX_DISPATCH_PER_TICK"] = "1"
    try:
        poller_tick.read_issues = lambda: tuple(candidates)
        dispatched = poller_tick.guarded_read_issues()
    finally:
        kbc.connect_closing = real_connect
        poller_tick.read_issues = real_read
        os.environ.pop("POLLER_MAX_DISPATCH_PER_TICK", None)

    ids = [s.identifier for s in dispatched]
    check("integration_skips_existing_and_sensitive", "LOL-90" not in ids and "LOL-82" not in ids, str(ids))
    check("integration_bound_one_per_tick", ids == ["LOL-91"], str(ids))
    post = conn.execute("SELECT count(*) FROM tasks WHERE title LIKE '[runtime] LOL-91%'").fetchone()[0]
    check("integration_created_no_extra_client_card", post == 0, f"new_cards={post}")

    print("== start(): execute the real defense-in-depth path (BUG-01 regression)")
    # BUG-01 (QA t_494664af): poller_tick.start() imported ExecutionBlocked from the
    # package root, which the pinned chain (ade1218) does not re-export -> ImportError on
    # every call, so the board re-check / idempotency key / human-context block never ran.
    # This section EXECUTES start(): the defect now fails the suite instead of passing it.
    sconn = build_conn()
    sconn.execute(
        "INSERT INTO tasks (id, title, body, assignee, status, priority, created_at, workspace_kind) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("t_start_existing", "[runtime] LOL-102",
         "Runtime dispatch for Linear LOL-102 (agent code-reviewer).",
         "code-reviewer", "blocked", 20, 1, "scratch"),
    )
    sconn.commit()

    @contextlib.contextmanager
    def start_connect(*, board=None):  # noqa: ANN001, ANN202
        yield sconn

    real_create = kb.create_task
    created: list[dict] = []

    def recording_create(conn_, **kwargs):  # noqa: ANN001, ANN202
        created.append(kwargs)
        return real_create(conn_, **kwargs)

    def ctx(issue_id, **over):  # noqa: ANN001, ANN202
        return StartContext(linear_issue_id=issue_id, **over)

    kbc.connect_closing = start_connect
    kb.create_task = recording_create
    try:
        # 1) existing card -> skip, returns the card id, no create_task
        got = poller_tick.start(ctx("LOL-102"))
        check("start_existing_card_skip", got == "t_start_existing" and not created,
              f"returned={got!r} create_calls={len(created)}")

        # 2) sensitive context -> blocked before any board write
        for label, c in (
            ("start_risk_high_blocked", ctx("LOL-QA1", risk="high")),
            ("start_risk_critical_blocked", ctx("LOL-QA1b", risk="critical")),
            ("start_execution_human_blocked", ctx("LOL-QA2", execution_mode="human")),
            ("start_env_production_blocked", ctx("LOL-QA3", environment="production")),
        ):
            try:
                poller_tick.start(c)
                check(label, False, "no exception raised")
            except ExecutionBlocked:
                check(label, True, "ExecutionBlocked")
            except Exception as exc:  # noqa: BLE001
                check(label, False, f"{type(exc).__name__}: {exc}")
        check("start_blocked_never_created", not created,
              f"create_calls={len(created)}")

        if os.environ.get("HERMES_DELEGATED_CHILD_CONTEXT"):
            skips.append("start() create path (in-memory DB write)")
            print("  [SKIP] start() eligible-creation: confined worker shell -> run --guard-test out-of-fence")
        else:
            # 3) eligible new issue -> real create_task on the in-memory board
            new_id = poller_tick.start(ctx("LOL-NEW", agent="code-reviewer"))
            kw = created[-1] if created else {}
            contract = (
                kw.get("title") == "[runtime] LOL-NEW"
                and kw.get("idempotency_key") == "runtime:linear:LOL-NEW"
                and kw.get("created_by") == "runtime-poller"
                and kw.get("assignee") == "code-reviewer"
                and kw.get("workspace_kind") == "scratch"
            )
            check("start_create_contract", contract,
                  f"id={new_id!r} title={kw.get('title')!r} key={kw.get('idempotency_key')!r} "
                  f"created_by={kw.get('created_by')!r} assignee={kw.get('assignee')!r}")
            row = sconn.execute(
                "SELECT count(*) FROM tasks WHERE idempotency_key='runtime:linear:LOL-NEW'"
            ).fetchone()[0]
            check("start_create_single_row", new_id and row == 1, f"id={new_id!r} rows={row}")
            # 4) second call now sees the created card -> dedup skip, same id, no extra row
            again = poller_tick.start(ctx("LOL-NEW", agent="code-reviewer"))
            rows = sconn.execute(
                "SELECT count(*) FROM tasks WHERE idempotency_key='runtime:linear:LOL-NEW'"
            ).fetchone()[0]
            check("start_dedup_after_create", again == new_id and rows == 1 and len(created) == 1,
                  f"again={again!r} rows={rows} create_calls={len(created)}")
    finally:
        kbc.connect_closing = real_connect
        kb.create_task = real_create

    failed = [label for label, ok, _ in checks if not ok]
    skipped = f" ({len(skips)} skipped)" if skips else ""
    print(f"RESULT: {'PASS' if not failed else 'FAIL'} ({len(checks) - len(failed)}/{len(checks)} checks){skipped}")
    if failed:
        print("FAILED: " + ", ".join(failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
