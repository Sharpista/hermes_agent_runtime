"""Structured event payload contract for Hermes runtime audit events.

The runtime treats Supabase ``agent_events.payload`` as a closed, non-secret JSON
object contract. Unknown fields are rejected before persistence so callers cannot
accidentally leak raw external payloads, logs, tokens, or PII.
"""

from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any
from urllib.parse import urlsplit


class PayloadValidationError(ValueError):
    """Raised when an event payload does not match the Spec 009 allowlist."""


_EVENT_FIELDS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "run.created": (frozenset(), frozenset()),
    "lock.acquired": (frozenset({"ttl_seconds", "expires_at"}), frozenset()),
    "run.started": (frozenset(), frozenset({"linear_status_to", "risk", "execution_mode", "environment"})),
    "agent.dispatched": (frozenset(), frozenset({"kanban_task_id", "dispatch_backend", "status", "assignee"})),
    "kanban.dispatched": (frozenset({"kanban_task_id", "status"}), frozenset({"assignee", "run_id", "linear_issue_id", "agent"})),
    "kanban.status_changed": (frozenset({"kanban_task_id", "status"}), frozenset({"assignee", "kanban_outcome", "run_id", "linear_issue_id", "agent"})),
    "kanban.completed": (frozenset({"kanban_task_id", "status"}), frozenset({"commit_sha", "pull_request_url", "railway_deployment_id", "tests_status", "review_status", "assignee", "kanban_outcome", "run_id", "linear_issue_id", "agent"})),
    "kanban.blocked": (frozenset({"kanban_task_id", "status"}), frozenset({"tests_status", "review_status", "assignee", "kanban_outcome", "run_id", "linear_issue_id", "agent"})),
    "kanban.failed": (frozenset({"kanban_task_id", "status", "kanban_outcome"}), frozenset({"assignee", "run_id", "linear_issue_id", "agent"})),
    "kanban.timeout": (frozenset({"kanban_task_id", "status"}), frozenset({"assignee", "run_id", "linear_issue_id", "agent"})),
    "tests.completed": (frozenset({"tests_status"}), frozenset({"command", "exit_code", "total", "passed", "failed", "skipped", "duration_ms", "artifact_path"})),
    "review.completed": (frozenset({"review_status"}), frozenset({"reviewer", "artifact_path", "pull_request_url"})),
    "deploy.requested": (frozenset({"environment"}), frozenset({"requested_by", "pull_request_url", "commit_sha"})),
    "deploy.completed": (frozenset({"railway_deployment_id", "environment", "deploy_status"}), frozenset({"deployment_url", "commit_sha", "duration_ms"})),
    "run.completed": (frozenset(), frozenset({"commit_sha", "pull_request_url", "railway_deployment_id", "tests_status", "review_status"})),
    "run.failed": (frozenset({"error"}), frozenset({"error_code", "kanban_task_id", "kanban_outcome"})),
    "run.blocked": (frozenset({"error"}), frozenset({"blocked_reason", "kanban_task_id", "tests_status", "review_status"})),
    "run.canceled": (frozenset(), frozenset({"canceled_by", "reason"})),
    "lock.released": (frozenset(), frozenset({"finished_status"})),
    "lock.rejected": (frozenset({"reason"}), frozenset({"attempted_run_id"})),
    "lock.expired": (frozenset({"expired_at"}), frozenset({"last_heartbeat_at", "ttl_seconds"})),
    "lock.recovered": (frozenset(), frozenset({"recovered_by", "previous_run_id"})),
}

_ENUMS = {
    "tests_status": frozenset({"passed", "failed", "skipped"}),
    "review_status": frozenset({"approved", "changes_requested", "blocked"}),
    "deploy_status": frozenset({"succeeded", "failed", "canceled"}),
    "environment": frozenset({"local", "preview", "staging", "production"}),
    "reason": frozenset({"RunConflict"}),
}
_DEPLOY_ENVIRONMENTS = frozenset({"preview", "staging", "production"})
_INT_FIELDS = frozenset({"ttl_seconds", "total", "passed", "failed", "skipped", "duration_ms", "exit_code"})
_URL_FIELDS = frozenset({"pull_request_url", "deployment_url"})
_ID_PATTERNS = {
    "run_id": re.compile(r"^run_[0-9A-HJKMNP-TV-Z]{26}$"),
    "kanban_task_id": re.compile(r"^t_[A-Za-z0-9_\-]+$"),
    "commit_sha": re.compile(r"^[0-9a-fA-F]{40}$"),
}
_SECRET_WORDS = (
    "token",
    "secret",
    "service_role",
    "authorization",
    "cookie",
    "connection string",
    "supabase_service_role_key",
    "linear_api_key",
    "github_token",
    "railway_token",
    "stack trace",
    "traceback",
    ".env",
)


def validate_event_payload(event_type: str, payload: Mapping[str, object] | None) -> dict[str, object]:
    """Return a normalized payload object or raise for contract violations."""

    if event_type not in _EVENT_FIELDS:
        raise PayloadValidationError(f"Unknown event type: {event_type}")
    if payload is None:
        candidate: Mapping[str, object] = {}
    elif isinstance(payload, Mapping):
        candidate = payload
    else:
        raise PayloadValidationError("Event payload must be a JSON object")

    required, optional = _EVENT_FIELDS[event_type]
    allowed = required | optional
    normalized: dict[str, object] = {}
    unknown = sorted(set(candidate) - allowed)
    if unknown:
        raise PayloadValidationError(f"Unsupported payload fields for {event_type}: {', '.join(unknown)}")

    for key, value in candidate.items():
        if value is None:
            continue
        _validate_value(event_type, key, value)
        normalized[key] = value

    missing = sorted(required - set(normalized))
    if missing:
        raise PayloadValidationError(f"Missing payload fields for {event_type}: {', '.join(missing)}")
    if event_type == "agent.dispatched" and normalized and "kanban_task_id" not in normalized:
        raise PayloadValidationError("agent.dispatched payload with dispatch metadata must include kanban_task_id")
    return normalized


def _validate_value(event_type: str, key: str, value: object) -> None:
    if isinstance(value, Mapping) or isinstance(value, list | tuple | set):
        raise PayloadValidationError(f"{key} must be a scalar value")
    if isinstance(value, str):
        if not value:
            raise PayloadValidationError(f"{key} must not be empty")
        lowered = value.lower()
        if any(word in lowered for word in _SECRET_WORDS):
            raise PayloadValidationError(f"{key} contains prohibited sensitive text")
    if key in _INT_FIELDS and (not isinstance(value, int) or isinstance(value, bool) or (key != "exit_code" and value < 0)):
        raise PayloadValidationError(f"{key} must be an integer")
    if key in _ENUMS and value not in _ENUMS[key] and (key != "reason" or event_type == "lock.rejected"):
        raise PayloadValidationError(f"{key} has an unsupported value")
    if key == "environment" and event_type.startswith("deploy.") and value not in _DEPLOY_ENVIRONMENTS:
        raise PayloadValidationError("deploy events require preview, staging, or production environment")
    pattern = _ID_PATTERNS.get(key)
    if pattern and (not isinstance(value, str) or not pattern.fullmatch(value)):
        raise PayloadValidationError(f"{key} has an invalid format")
    if key in _URL_FIELDS:
        _validate_https_url(key, value)


def _validate_https_url(key: str, value: Any) -> None:
    if not isinstance(value, str):
        raise PayloadValidationError(f"{key} must be a string URL")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc:
        raise PayloadValidationError(f"{key} must be an HTTPS URL")
    if parsed.query or parsed.fragment:
        raise PayloadValidationError(f"{key} must not contain query string or fragment")


__all__ = ["PayloadValidationError", "validate_event_payload"]
