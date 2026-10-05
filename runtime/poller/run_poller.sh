#!/bin/sh
# LOL-97 / t_a4d0e749 - permanent, reproducible entrypoint for the Hermes runtime poller.
#
# One interpreter that imports, together:
#   * Hermes                (hermes_cli / hermes_cli.kanban_db - the Kanban board API)
#   * hermes-agent-runtime  (the merged orchestrator chain, origin/main @ ade1218)
#   * the Kanban SQLite API itself
#
# Stability decisions (LOL-97 / t_a4d0e749):
#   * interpreter: resolved dynamically from the pinned install's facts.json
#     (survives `hermes update` generation bumps), with the LOL-79 path as fallback.
#   * entrypoint: the chain is pinned to the MERGED commit ade1218 - read-only
#     extraction under ~/.hermes/runtime/chain/ade1218 - so the unit no longer
#     depends on the volatile worktree /home/alexandre/hermes-agent-runtime/.worktrees/t_0f04a754.
#   * HERMES_HOME: explicit (the shared root the gateway/dispatcher use), not inferred.
#
# It MUST run outside a confined Hermes worker shell: a delegate_task child carries
# HERMES_DELEGATED_CHILD_CONTEXT and cannot mutate the board, so the mutating poller
# refuses to start there. Launch it from a systemd user unit / cron outside Hermes.
#
# Usage:
#   run_poller.sh --probe                 # read-only environment probe (LOL-79)
#   run_poller.sh --canary                # read-only permanent-unit canary (LOL-97)
#   run_poller.sh --gates-test            # read-only CR-1 gate regression (LOL-101)
#   run_poller.sh --guard-test            # read-only dedup/bound guard tests (LOL-102)
#   run_poller.sh <module-or-script.py> [args...]
#
set -eu

# --- explicit home (never inferred from the calling shell) ---------------------
# CR-2 / LOL-101: HERMES_HOME is PINNED to HERMES_ROOT. The previous
# `: "${HERMES_HOME:=$HERMES_ROOT}"` kept an inherited HERMES_HOME (e.g. a
# profile home exported by a QA shell), which made --canary profile-dependent
# (BUG-01). The production unit already fixes Environment=HERMES_HOME; this
# makes the wrapper match that contract regardless of the caller's env.
HERMES_ROOT="${HERMES_ROOT:-/home/alexandre/.hermes}"
HERMES_HOME="$HERMES_ROOT"
export HERMES_HOME

HERMES_CHECKOUT="${HERMES_CHECKOUT:-$HERMES_ROOT/hermes-agent}"

# --- pinned chain (merged PR #5 -> origin/main) --------------------------------
CHAIN_PIN="${CHAIN_PIN:-ade12189336abf5d28d1c9b1a8ada823577bf834}"
CHAIN_ROOT="${CHAIN_ROOT:-$HERMES_ROOT/runtime/chain/ade1218}"
CHAIN_SRC="${CHAIN_SRC:-$CHAIN_ROOT/src}"

# --- stable interpreter: resolve from the install's facts.json -----------------
# The install key is sha256(<checkout>)[:16] (pm/environments.py::install_key); the
# venv path lives in facts.json -> packages.venv.environment. Falls back to the
# interpreter pinned at preparation time (LOL-79) when the install cannot be resolved.
resolve_interpreter() {
    if [ -n "${POLLER_PYTHON:-}" ]; then
        printf '%s\n' "$POLLER_PYTHON"; return 0
    fi
    key=$(printf '%s' "$HERMES_CHECKOUT" | sha256sum | cut -c1-16)
    facts="$HERMES_ROOT/installs/$key/facts.json"
    if [ -f "$facts" ]; then
        env_dir=$(grep -o '"environment"[[:space:]]*:[[:space:]]*"[^"]*"' "$facts" \
                  | head -n 1 | grep -o '/[^"]*')
        if [ -n "${env_dir:-}" ] && [ -x "$env_dir/bin/python" ]; then
            printf '%s\n' "$env_dir/bin/python"; return 0
        fi
    fi
    printf '%s\n' \
        "$HERMES_ROOT/installs/974d540f82c0c176/environments/496d61b5a8854999ba0e681f1ec5b777/venv/bin/python"
}
POLLER_PYTHON="$(resolve_interpreter)"

# Hermes code comes from the LIVE checkout (the tree the running gateway imports);
# the pinned chain supplies hermes_agent_runtime. Order matters: live checkout first.
export PYTHONPATH="$HERMES_CHECKOUT:$CHAIN_SRC${PYTHONPATH:+:$PYTHONPATH}"
export HERMES_KANBAN_BOARD="${HERMES_KANBAN_BOARD:-lolcoach}"
export CHAIN_PIN

PROBE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

case "${1:-}" in
    --probe)
        exec "$POLLER_PYTHON" "$PROBE_DIR/probe_env.py"
        ;;
    --canary)
        exec "$POLLER_PYTHON" "$PROBE_DIR/canary_readonly.py"
        ;;
    --gates-test)
        exec "$POLLER_PYTHON" "$PROBE_DIR/test_read_task_gates.py"
        ;;
    --guard-test)
        exec "$POLLER_PYTHON" "$PROBE_DIR/test_poller_dispatch_guard.py"
        ;;
esac

if [ "$#" -eq 0 ]; then
    echo "usage: $0 --probe | --canary | --gates-test | <module-or-script.py> [args...]" >&2
    exit 2
fi

# The mutating poller cannot run inside a confined worker shell.
if [ -n "${HERMES_DELEGATED_CHILD_CONTEXT:-}" ]; then
    echo "run_poller.sh: refusing to run inside a confined Hermes worker shell" >&2
    echo "  HERMES_DELEGATED_CHILD_CONTEXT=${HERMES_DELEGATED_CHILD_CONTEXT}" >&2
    echo "  launch from the gateway/orchestrator or a systemd/cron unit outside Hermes" >&2
    exit 3
fi

exec "$POLLER_PYTHON" "$@"
