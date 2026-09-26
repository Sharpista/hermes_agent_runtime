"""Hermes v0.21.5 Kanban CLI bridge (server-side only).

This module uses the installed CLI as the sole owner of Kanban writes and
dispatcher spawn. The coordinator merely creates a card and observes its
durable task/run JSON until a terminal outcome is recorded.
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any

from .kanban import KanbanDispatcherAdapter, KanbanRunSnapshot, KanbanTaskSnapshot
from .orchestrator import StageRequest
from .runtime import ExecutionBlocked, ExecutionResult


PROFILES = {"orchestrator": "orquestrador"}
CODE_STAGES = frozenset({"implement", "candidate_commit", "quality", "review", "rework", "publish_pr"})


def _cli(*arguments: str, body: str | None = None) -> Any:
    board = os.getenv("HERMES_KANBAN_BOARD", "lolcoach")
    env = os.environ.copy()
    # The Hermes worker never needs the coordinator's privileged credentials.
    for name in ("LINEAR_API_KEY", "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_SECRET_KEY"):
        env.pop(name, None)
    try:
        completed = subprocess.run(
            [os.getenv("HERMES_CLI", "hermes"), "kanban", "--board", board, *arguments],
            input=body, text=True, capture_output=True, env=env, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError("Hermes CLI unavailable") from None
    if completed.returncode:
        # stderr/stdout can contain task text or credentials: keep only the exit code.
        raise RuntimeError(f"Hermes CLI failed (exit {completed.returncode})")
    try:
        return json.loads(completed.stdout)
    except (ValueError, TypeError):
        raise RuntimeError("Hermes CLI did not return JSON") from None


def _task_body(request: StageRequest) -> str:
    lines = [
        f"Linear: {request.issue.identifier} — {request.issue.title}",
        f"Etapa: {request.stage}; run_id: {request.context.run_id}",
        f"Critérios e escopo:\n{request.issue.description}",
        "Registre o resultado via kanban_complete com metadata estruturado.",
        "Para commit, QA, review e PR, inclua commit_sha de 40 caracteres.",
        "QA inclui tests_status=passed; revisão inclui review_status=approved.",
        "Publicação inclui pull_request_url HTTPS de PR real; não marque sucesso só pelo spawn.",
    ]
    if request.candidate_sha:
        lines.append(f"SHA candidato/anterior: {request.candidate_sha}")
    if request.branch:
        lines.append(f"Branch: {request.branch}")
    return "\n\n".join(lines)


def _start(request: StageRequest) -> str:
    profile = PROFILES.get(request.context.agent, request.context.agent)
    arguments = ["create", f"{request.issue.identifier}: {request.stage}",
                 "--assignee", profile,
                 "--idempotency-key", f"{request.context.run_id}:{request.stage}",
                 "--body-file", "-", "--json"]
    if request.stage == "implement":
        project = os.getenv("HERMES_KANBAN_PROJECT")
        if not project:
            raise ExecutionBlocked("HERMES_KANBAN_PROJECT is required for implementation")
        arguments.extend(["--project", project])
    elif request.stage in CODE_STAGES:
        if not request.workspace_path or not os.path.isabs(request.workspace_path):
            raise ExecutionBlocked("A verified worktree path is required for this stage")
        arguments.extend(["--workspace", f"dir:{request.workspace_path}"])
    if request.stage == "publish_pr":
        repository = os.getenv("HERMES_GITHUB_REPOSITORY")
        if not repository or repository.count("/") != 1:
            raise ExecutionBlocked("HERMES_GITHUB_REPOSITORY is required for PR publication")
        arguments.extend(["--completion-contract", repository])
    data = _cli(*arguments, body=_task_body(request))
    task_id = data.get("id") if isinstance(data, dict) else None
    if not isinstance(task_id, str) or not task_id.startswith("t_"):
        raise RuntimeError("Hermes create returned no task id")
    return task_id


def _read_task(task_id: str) -> KanbanTaskSnapshot:
    document = _cli("show", task_id, "--json")
    if not isinstance(document, dict) or not isinstance(document.get("task"), dict):
        raise RuntimeError("Hermes show returned no task")
    task = document["task"]
    if task.get("id") != task_id:
        raise RuntimeError("Hermes returned a different task")
    runs = document.get("runs") or []
    latest = runs[-1] if runs else None
    metadata = latest.get("metadata") if isinstance(latest, dict) else None
    run = None if latest is None else KanbanRunSnapshot(
        status=latest.get("status"), outcome=latest.get("outcome"),
        summary=latest.get("summary"), metadata=metadata if isinstance(metadata, dict) else {},
    )
    return KanbanTaskSnapshot(
        task_id=task_id, status=task["status"], assignee=task.get("assignee"),
        workspace_path=task.get("workspace_path"), branch=task.get("branch_name"),
        latest_run=run,
    )


def dispatch_stage(request: StageRequest) -> ExecutionResult:
    timeout = float(os.getenv("HERMES_STAGE_TIMEOUT_SECONDS", "3600"))
    adapter = KanbanDispatcherAdapter(lambda _: _start(request), _read_task,
                                      poll_seconds=5, timeout_seconds=timeout)
    return adapter(request.context)
