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

Publication requires an explicit process flag (`--allow-pr-publish`), which
must only be used for an authorized scope. Even after PR creation the Linear
issue remains `In Review`: CI, acceptance and any deployment are separate
gates. Production issues, critical risk, and human execution labels are
blocked by the runtime. The coordinator never deploys to Railway.

The live Hermes bridge is not included because the installed v0.21.2 dispatcher
and Kanban SQLite schema are outside this repository. A server deployment must
provide the callable and test it against the installed CLI/board contract.
An interrupted sequence after `In Progress` requires reconciliation of its
recorded runs and candidate SHA before resuming; the poller only starts new
`Todo` issues. Do not manually reset it to `Todo` without that reconciliation.
