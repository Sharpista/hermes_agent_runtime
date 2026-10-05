"""Read-only ExecStart self-test for the permanent poller unit (LOL-97 / t_a4d0e749).

Proves the real ExecStart target imports cleanly under the unit's environment
without executing a tick -- it never calls ``main()``, never reads Linear/Supabase
and never touches the board. Run it through run_poller.sh from a systemd unit:

    run_poller.sh /home/alexandre/.hermes/runtime/poller/selftest_tick_import.py
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

POLLER_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(POLLER_DIR))


def main() -> int:
    print(f"interpreter: {sys.executable}")
    print(f"HERMES_HOME: {os.environ.get('HERMES_HOME')}")
    print(f"HERMES_KANBAN_BOARD: {os.environ.get('HERMES_KANBAN_BOARD')}")

    import poller_tick  # module-level only; main() is NOT called
    print(f"poller_tick: imported from {poller_tick.__file__}")
    print(f"poller_tick.read_issues: {callable(poller_tick.read_issues)}")
    print(f"poller_tick.start: {callable(poller_tick.start)}")

    for name in ("hermes_cli.kanban_db", "hermes_agent_runtime", "hermes_agent_runtime.orchestrator"):
        mod = importlib.import_module(name)
        print(f"import OK {name}: {getattr(mod, '__file__', '?')}")

    print("RESULT: PASS (execstart target imports cleanly, no tick executed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
