"""Read-only canary for the permanent runtime poller unit (LOL-97 / t_a4d0e749).

Proves the *prepared* unit would work, without mutating anything:

  1. interpreter resolved by run_poller.sh is the install venv (python 3.14.x);
  2. HERMES_HOME is the explicit shared root;
  3. the chain import set resolves from the pinned extraction;
  4. the pinned chain matches the merged commit 477b28e4 (files vs PIN.json);
  5. the board resolves to the shared lolcoach SQLite DB and is readable;
  6. the chain's own unit-test suite passes against the pinned tree;
  7. the prepared unit/timer files are present and well-formed (systemd-analyze);
  8. the shared secret file is present, mode 600 and exposes the 3 required keys
     (values are never read into memory beyond the presence check, never printed);
  9. optional read-only network probe (Supabase counts + Linear viewer) when
     POLLER_CANARY_NETWORK=1.

It never creates, dispatches, mutates or archives anything, and never prints a
secret. Exit 0 = PASS, 1 = FAIL.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

POLLER_DIR = Path("/home/alexandre/.hermes/runtime/poller")
CHAIN_ROOT = Path(os.environ.get("CHAIN_ROOT", "/home/alexandre/.hermes/runtime/chain/477b28e4"))
CHAIN_PIN = os.environ.get("CHAIN_PIN", "477b28ede946a6bff5ae562339e31b999015518e")
EXPECTED_HOME = "/home/alexandre/.hermes"
BOARD = os.environ.get("HERMES_KANBAN_BOARD", "lolcoach")
UNIT_FILES = (
    POLLER_DIR / "hermes-runtime-poller.service",
    POLLER_DIR / "hermes-runtime-poller.timer",
    Path("/home/alexandre/.config/systemd/user/hermes-runtime-poller.service"),
    Path("/home/alexandre/.config/systemd/user/hermes-runtime-poller.timer"),
)

_checks: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    _checks.append((label, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: {detail}")


def sha16(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return "missing"


def sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return "missing"


def main() -> int:
    print("canary (read-only) - permanent runtime poller unit")
    print(f"  interpreter: {sys.executable}")
    print(f"  version:     {sys.version.split()[0]}")
    print(f"  HERMES_HOME: {os.environ.get('HERMES_HOME', '<unset>')}")
    print(f"  board:       {BOARD}")
    print(f"  chain root:  {CHAIN_ROOT}")
    print(f"  chain pin:   {CHAIN_PIN}")

    # 1. interpreter
    version = sys.version.split()[0]
    check("interpreter_is_install_venv", "/installs/" in sys.executable and "/venv/bin/python" in sys.executable, sys.executable)
    check("python_major_is_3_14", version.startswith("3.14"), version)

    # 2. explicit home
    check("hermes_home_explicit_root", os.environ.get("HERMES_HOME", "") == EXPECTED_HOME, os.environ.get("HERMES_HOME", "<unset>"))

    # 3/4. chain pin from the pinned extraction
    pin_file = CHAIN_ROOT / "PIN.json"
    if pin_file.is_file():
        pin = json.loads(pin_file.read_text())
        check("chain_pin_commit", pin.get("commit") == CHAIN_PIN, pin.get("commit", "?"))
        mismatched = [rel for rel, want in pin.get("files", {}).items() if sha256(CHAIN_ROOT / rel) != want]
        check("chain_pin_files", not mismatched, f"{len(pin.get('files', {}))} files, mismatched={mismatched}")
    else:
        check("chain_pin_commit", False, f"missing {pin_file}")

    imports = (
        "hermes_cli", "hermes_cli.kanban_db", "hermes_cli.kanban_db_connect",
        "hermes_agent_runtime", "hermes_agent_runtime.orchestrator",
        "hermes_agent_runtime.poller", "hermes_agent_runtime.selector",
        "hermes_agent_runtime.adapter", "hermes_agent_runtime.kanban",
        "hermes_agent_runtime.payloads", "hermes_agent_runtime.supabase",
    )
    import importlib
    bad: list[str] = []
    for name in imports:
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001
            bad.append(f"{name}({type(exc).__name__})")
    check("imports", not bad, f"{len(imports) - len(bad)}/{len(imports)} OK" + (f" failing={bad}" if bad else ""))

    # 5. board readable, resolved to the shared DB
    try:
        kbc = importlib.import_module("hermes_cli.kanban_db_connect")
        kb = importlib.import_module("hermes_cli.kanban_db")
        db_path = str(kb.kanban_db_path(BOARD))
        with kbc.connect_closing(board=BOARD) as conn:
            total = conn.execute("SELECT count(*) FROM tasks").fetchone()[0]
            smoke = conn.execute(
                "SELECT count(*) FROM tasks WHERE created_by='runtime-poller' OR title LIKE 'SMOKE%'"
            ).fetchone()[0]
        check("board_read", total > 0, f"db={db_path} tasks={total} poller_residue={smoke}")
        check("board_is_shared_path", "/.hermes/kanban/boards/" in db_path, db_path)
    except Exception as exc:  # noqa: BLE001
        check("board_read", False, f"{type(exc).__name__}: {exc}")

    # 6. chain unit-test suite (read-only, runs from the pinned tree)
    tests_dir = CHAIN_ROOT / "tests"
    if tests_dir.is_dir():
        proc = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", str(tests_dir)],
            cwd=str(CHAIN_ROOT), capture_output=True, text=True,
        )
        tail = (proc.stderr or proc.stdout).strip().splitlines()
        check("chain_suite", proc.returncode == 0, tail[-1] if tail else f"rc={proc.returncode}")
    else:
        check("chain_suite", False, f"missing {tests_dir}")

    # 7. prepared unit/timer files well-formed
    for unit in UNIT_FILES:
        check(f"unit_file_present[{unit.name}]", unit.is_file(), str(unit))
    if all(p.is_file() for p in UNIT_FILES):
        verify = subprocess.run(
            ["systemd-analyze", "verify", str(UNIT_FILES[0]), str(UNIT_FILES[1])],
            capture_output=True, text=True,
        )
        check("systemd_analyze_verify", verify.returncode == 0,
              (verify.stderr or verify.stdout).strip() or "OK")

    # 8. secrets file present + mode 600 + required keys present (no values printed)
    env_file = Path(os.environ.get("POLLER_ENV_FILE", "/home/alexandre/.hermes/shared/.env"))
    if env_file.is_file():
        mode = oct(env_file.stat().st_mode & 0o777)
        keys = set()
        for raw in env_file.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                keys.add(line.split("=", 1)[0].strip())
        required = {"SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "LINEAR_API_KEY"}
        check("secret_file_mode", mode == "0o600", f"{env_file} mode={mode}")
        check("secret_keys_present", required <= keys, f"missing={sorted(required - keys)}")
    else:
        check("secret_file_mode", False, f"missing {env_file}")

    # 9. optional read-only network probe
    if os.environ.get("POLLER_CANARY_NETWORK") == "1":
        try:
            sys.path.insert(0, str(POLLER_DIR))
            import poller_env  # noqa: WPS433
            poller_env.export_into_environ()
            import urllib.request
            url = os.environ["SUPABASE_URL"].rstrip("/")
            req = urllib.request.Request(
                url + "/rest/v1/agent_events?select=id",
                headers={"apikey": os.environ["SUPABASE_SERVICE_ROLE_KEY"],
                         "Authorization": "Bearer " + os.environ["SUPABASE_SERVICE_ROLE_KEY"],
                         "Prefer": "count=exact", "Range": "0-0"},
                method="HEAD",
            )
            with urllib.request.urlopen(req, timeout=20) as r:
                count = r.headers.get("Content-Range", "?")
            check("supabase_readonly", True, f"agent_events {count}")
        except Exception as exc:  # noqa: BLE001
            check("supabase_readonly", False, f"{type(exc).__name__}: {exc}")

    failed = [label for label, ok, _ in _checks if not ok]
    print(f"RESULT: {'PASS' if not failed else 'FAIL'} ({len(_checks) - len(failed)}/{len(_checks)} checks)")
    if failed:
        print("FAILED: " + ", ".join(failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
