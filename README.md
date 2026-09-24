# Hermes Agent Runtime

Reusable server-side execution lifecycle for Linear issues backed by the existing
LoLCoach Supabase tables (`agent_runs`, `agent_execution_locks`, `agent_events`).

This repository contains a runtime boundary, **not** the installed Hermes dispatcher.
The caller supplies a dispatcher callback and, optionally, a Linear status callback.
It does not poll Linear, create GitHub PRs, deploy to Railway, or mark issues Done.
Wire those steps into the installed orchestrator after its source is available.

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
