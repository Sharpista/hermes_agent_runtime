"""Server-side PostgREST adapter. Never import this module into a browser client."""

from __future__ import annotations

import json
import os
from typing import Mapping
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .runtime import ExecutionContext, RunConflict


class SupabaseStore:
    def __init__(self, url: str, service_key: str, *, timeout: float = 15):
        if not url.startswith("https://") or not service_key:
            raise ValueError("HTTPS Supabase URL and server-side key are required")
        self.url = url.rstrip("/")
        self._key = service_key
        self.timeout = timeout

    @classmethod
    def from_environment(cls) -> "SupabaseStore":
        return cls(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])

    def _post(self, path: str, body: Mapping[str, object]) -> object:
        request = Request(
            self.url + "/rest/v1/" + path,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "apikey": self._key,
                "Authorization": "Bearer " + self._key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                data = response.read()
            return json.loads(data) if data else None
        except HTTPError as exc:
            # Do not put raw response bodies or credentials in exceptions or logs.
            if exc.code == 409:
                raise RunConflict("Active run already exists for this issue") from None
            raise RuntimeError(f"Supabase request failed (HTTP {exc.code})") from None

    def claim(self, context: ExecutionContext, ttl_seconds: int) -> None:
        result = self._post("rpc/hermes_claim_run", {
            "p_run_id": context.run_id,
            "p_issue_id": context.linear_issue_id,
            "p_agent": context.agent,
            "p_risk": context.risk,
            "p_mode": context.execution_mode,
            "p_environment": context.environment,
            "p_ttl_seconds": ttl_seconds,
        })
        if result is not True:
            raise RunConflict("Active run already exists for this issue")

    def heartbeat(self, context: ExecutionContext, ttl_seconds: int) -> bool:
        return self._post("rpc/hermes_heartbeat_run", {
            "p_run_id": context.run_id, "p_issue_id": context.linear_issue_id,
            "p_ttl_seconds": ttl_seconds,
        }) is True

    def event(self, context: ExecutionContext, kind: str, payload: Mapping[str, object]) -> None:
        self._post("agent_events", {
            "run_id": context.run_id,
            "linear_issue_id": context.linear_issue_id,
            "agent": context.agent,
            "event_type": kind,
            "payload": dict(payload),
        })

    def finish(self, context: ExecutionContext, status: str, fields: Mapping[str, object]) -> None:
        result = self._post("rpc/hermes_finish_run", {
            "p_run_id": context.run_id, "p_issue_id": context.linear_issue_id,
            "p_status": status, "p_fields": dict(fields),
        })
        if result is not True:
            raise RuntimeError("Run no longer owns the issue lock")

    def recover(self, issue_id: str) -> bool:
        return self._post("rpc/hermes_recover_expired_lock", {"p_issue_id": issue_id}) is True
