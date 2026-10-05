"""
Read-only environment probe for the Hermes runtime poller (LOL-79 / t_0d5d2c4c).

Proves, for the interpreter that runs it, that a single process can import:
  (1) Hermes      - hermes_cli / hermes_cli.kanban_db (the board API),
  (2) hermes_agent_runtime - the pinned orchestrator chain (poller/selector/adapter),
  (3) the Kanban  - a read-only task read from the `lolcoach` board.

It only reads. It never creates, dispatches, mutates or archives anything, and
it never prints secrets (credentials are not read at all).
"""
from __future__ import annotations

import hashlib
import importlib
import json
import os
import subprocess
import sys

CHAIN_WORKTREE = "/home/alexandre/hermes-agent-runtime/.worktrees/t_0f04a754"
CHAIN_SHA_EXPECTED = "dc3f7dfcc733caa167e2173ab5fbe4da1d680b78"
CHAIN_FILES = (
    "src/hermes_agent_runtime/__init__.py",
    "src/hermes_agent_runtime/adapter.py",
    "src/hermes_agent_runtime/orchestrator.py",
    "src/hermes_agent_runtime/poller.py",
    "src/hermes_agent_runtime/selector.py",
    "src/hermes_agent_runtime/runtime.py",
)


def line(label, value):
    print(f"{label}: {value}")


def try_import(name):
    try:
        mod = importlib.import_module(name)
        return True, getattr(mod, "__file__", "?"), None
    except Exception as exc:  # noqa: BLE001
        return False, None, f"{type(exc).__name__}: {exc}"


def sha16(path):
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()[:16]
    except OSError:
        return "missing"


def chain_commit():
    try:
        out = subprocess.run(
            ["git", "-C", CHAIN_WORKTREE, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=20,
        )
        return out.stdout.strip() if out.returncode == 0 else f"rc={out.returncode}"
    except Exception as exc:  # noqa: BLE001
        return f"ERR:{type(exc).__name__}"


def main() -> int:
    line("interpreter", sys.executable)
    line("version", sys.version.split()[0])
    line("HERMES_HOME", os.environ.get("HERMES_HOME", ""))
    line("HERMES_KANBAN_BOARD", os.environ.get("HERMES_KANBAN_BOARD", ""))
    line("HERMES_KANBAN_DB", os.environ.get("HERMES_KANBAN_DB", ""))
    line("HERMES_DELEGATED_CHILD_CONTEXT", os.environ.get("HERMES_DELEGATED_CHILD_CONTEXT", "<unset>"))

    ok_all = True
    for name in (
        "hermes_cli",
        "hermes_cli.kanban_db",
        "hermes_cli.kanban_db_connect",
        "hermes_cli.kanban_db_dispatch",
        "hermes_agent_runtime",
        "hermes_agent_runtime.orchestrator",
        "hermes_agent_runtime.poller",
        "hermes_agent_runtime.selector",
        "hermes_agent_runtime.adapter",
        "hermes_agent_runtime.kanban",
        "hermes_agent_runtime.payloads",
        "hermes_agent_runtime.supabase",
    ):
        ok, path, err = try_import(name)
        if ok:
            line(f"  OK   {name}", path)
        else:
            ok_all = False
            line(f"  FAIL {name}", err)

    commit = chain_commit()
    line("chain_commit", commit)
    line("chain_commit_expected", CHAIN_SHA_EXPECTED)
    if commit != CHAIN_SHA_EXPECTED:
        ok_all = False
        line("chain_commit_match", "MISMATCH")
    for rel in CHAIN_FILES:
        line(f"  sha256[:16] {rel}", sha16(os.path.join(CHAIN_WORKTREE, rel)))

    symbols = {}
    try:
        for modname, wanted in (
            ("hermes_agent_runtime.orchestrator", ("RuntimeOrchestrator", "OrchestratorRunResult")),
            ("hermes_agent_runtime.poller", ("LinearIssuePoller",)),
            ("hermes_agent_runtime.selector", ("LinearIssueSelector",)),
            ("hermes_agent_runtime.adapter", ("RuntimeDispatchAdapter", "kanban_dispatch_adapter")),
        ):
            mod = importlib.import_module(modname)
            for sym in wanted:
                symbols[sym] = hasattr(mod, sym)
        line("chain_symbols", json.dumps(symbols, sort_keys=True))
        if not all(symbols.values()):
            ok_all = False
    except Exception as exc:  # noqa: BLE001
        ok_all = False
        line("chain_symbols", f"FAIL {type(exc).__name__}: {exc}")

    # Read-only board probe via the real Hermes Kanban API.
    try:
        kbc = importlib.import_module("hermes_cli.kanban_db_connect")
        kb = importlib.import_module("hermes_cli.kanban_db")
        with kbc.connect_closing(board=os.environ.get("HERMES_KANBAN_BOARD", "lolcoach")) as conn:
            task = kb.get_task(conn, "t_0d5d2c4c")
            total = conn.execute("SELECT count(*) FROM tasks").fetchone()[0]
            line("board_read", f"board={os.environ.get('HERMES_KANBAN_BOARD', 'lolcoach')} tasks={total} t_0d5d2c4c={getattr(task, 'status', None)}")
    except Exception as exc:  # noqa: BLE001
        ok_all = False
        line("board_read", f"FAIL {type(exc).__name__}: {exc}")

    line("RESULT", "PASS" if ok_all else "FAIL")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
