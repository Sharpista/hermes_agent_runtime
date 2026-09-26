# Hermes Agent Runtime

Reusable server-side execution lifecycle for Linear issues backed by the existing
LoLCoach Supabase tables (`agent_runs`, `agent_execution_locks`, `agent_events`).

This repository contains a runtime boundary and a server-side Linear coordinator.
The coordinator polls `Todo` issues, routes `agent:*` labels, claims a Supabase
run/lock for each stage, and delegates to a caller-supplied Hermes Kanban bridge.
It does not replace the installed dispatcher, perform deploys, or mark issues Done.

## Setup

Python 3.11+; no third-party runtime dependencies. Install locally with
`python -m pip install -e .`. Set `SUPABASE_URL` and
`SUPABASE_SERVICE_ROLE_KEY` in the Hermes server environment only.
Never place the service key in a browser, commit, issue or log.

Review `sql/001_runtime_functions.sql`, then apply it to the LoLCoach database
as a migration after confirming the three tables exist. The functions use
transactions and unique constraints for atomic claim, heartbeat, finish and
expired-lock recovery. Only `service_role` receives EXECUTE privileges.

```python
from hermes_agent_runtime import AgentRuntime, ExecutionResult, Issue, SupabaseStore

runtime = AgentRuntime(SupabaseStore.from_environment())

def dispatch(context):
    # Adapt the installed Hermes dispatcher; retain context.run_id at every step.
    # Return only after the required quality gates have completed.
    return ExecutionResult(tests_status="passed", review_status="approved")

runtime.execute(
    Issue("LOL-56", frozenset({"agent:backend", "execution:auto", "env:local"})),
    dispatch,
    move_status=lambda issue_id, status: linear_update_status(issue_id, status),
)
```

For Hermes Kanban workers, keep the installed dispatcher as the owner of task
spawn/run bookkeeping and inject it through `KanbanDispatcherAdapter`. The
adapter treats a spawn as only "dispatched"; it returns an `ExecutionResult`
only after a read-only task snapshot reaches a terminal outcome with explicit
`tests_status` and `review_status` metadata.

Call `runtime.recover_expired_issue(issue_id)` from the orchestrator recovery
loop before trying to claim an expired issue again. A conflict leaves the
existing run intact. A heartbeat failure triggers a failed-run transition when
the worker still owns the lock. If another worker already recovered the expired
lock, the old worker's final update is rejected for orchestrator review.

The wrapper is synchronous because the installed Hermes dispatch contract is
unknown. For async dispatchers, use a dedicated worker thread or adapt this
boundary instead of calling `asyncio.run` inside an existing event loop.

## Verification

```sh
PYTHONPATH=src python -m unittest discover -s tests -v
python -m compileall -q src
```

The tests use an in-memory store and do not touch the live Supabase project.
Before production use, test the SQL migration and two concurrent claims in a
development database, then verify expiry recovery and service-role-only access.

## Operational coordinator

The CLI requires `LINEAR_API_KEY`, `SUPABASE_URL`, and
`SUPABASE_SERVICE_ROLE_KEY` in the server process environment. For polling,
also set `LINEAR_TEAM_ID` or pass `--team-id`. The `--adapter` value names a
Python callable in the **installed Hermes environment**:

```sh
python -m hermes_agent_runtime --team-id YOUR_TEAM_UUID \
  --adapter hermes_bridge:dispatch_stage --interval 30
```

`dispatch_stage(request: StageRequest) -> ExecutionResult` must hand a task to
the installed Hermes profile dispatcher, retain `request.context.run_id`, and
wait for its final Kanban snapshot. The existing `KanbanDispatcherAdapter` can
perform the waiting when given real `start` and `read_task` callbacks. Each
request contains the Linear issue, agent, stage and candidate SHA. Do not return
success based on spawn alone. The stages are `implement`, `candidate_commit`,
`quality`, `review`, and `publish_pr`. For absent or multiple `agent:*` labels,
the `classify` stage goes to `orchestrator`; a specialist-only issue uses
`specialist` and remains pending human/orchestrator review.
The installed Hermes profile is called `orquestrador`; map the runtime alias
`orchestrator` to that profile in the bridge.
Failed QA or requested review changes return to the implementing profile via
`rework`, then require a new candidate commit and fresh QA/review on its SHA.
The default limit is two correction rounds; an exhausted loop stays blocked.

Publication requires an explicit process flag (`--allow-pr-publish`), which
must only be used for an authorized scope. Even after PR creation the Linear
issue remains `In Review`: CI, acceptance and any deployment are separate
gates. Production issues, critical risk, and human execution labels are
blocked by the runtime. The coordinator never deploys to Railway.

## Hermes v0.21.5 bridge

`hermes_agent_runtime.hermes_bridge:dispatch_stage` uses the installed Kanban
CLI and the board's task/run JSON. It does not spawn workers itself. Configure
`HERMES_KANBAN_BOARD=lolcoach`, `HERMES_KANBAN_PROJECT=<Hermes project slug>`
and `HERMES_GITHUB_REPOSITORY=OWNER/REPO` for the relevant project. The first
implementation card gets a project worktree; subsequent stages reuse the
verified `workspace_path` and branch from that card. Ensure the Hermes gateway
already owns dispatch on this board; do not run a second daemon. Verify the
project slug with `hermes project list` on the server before activation.

Start with one authorized `Todo` test issue and the matching server-side
secrets, then inspect `hermes kanban --board lolcoach show <task_id> --json`
and the Supabase run timeline. After successful smoke, configure the polling
process under your existing service manager:

```sh
python -m hermes_agent_runtime --issue LOL-TEST \
  --adapter hermes_agent_runtime.hermes_bridge:dispatch_stage \
  --allow-pr-publish
```

The bridge contract is based on Hermes v0.21.5 (`a7059225`). It has unit tests
using the exact documented JSON field names, but has not yet been exercised on
the user's VPS. An interrupted sequence after `In Progress` requires
reconciliation of recorded runs and candidate SHA before resuming; the poller
only starts new `Todo` issues. Do not reset an issue to `Todo` blindly.
