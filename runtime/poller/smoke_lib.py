"""Shared helpers for the LOL-80 / t_153f63a4 final chain smoke.

Loads secrets from ~/.hermes/shared/.env at runtime (never prints them) and
provides minimal HTTP/GraphQL clients for Supabase PostgREST and Linear.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SHARED_ENV = Path("/home/alexandre/.hermes/shared/.env")

# Linear fixture constants (team Lolcoach).
TEAM_ID = "ddc0d73d-2dcc-413a-89dd-7c840c1bbb4b"
STATE_TODO = "58e53f4e-216d-4461-abdf-e40dc2a74ddb"
STATE_IN_PROGRESS = "f7acf339-dc31-433b-8e26-574a2cae1aba"
STATE_IN_REVIEW = "39604746-b04c-4488-a8b3-2a1bdfbade04"
STATE_BLOCKED = os.environ.get("POLLER_LINEAR_STATE_BLOCKED", "142dc11e-7d2b-45d6-8122-7f0015d26c7b")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_env(path: Path = SHARED_ENV) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def export_into_environ(path: Path = SHARED_ENV) -> dict[str, str]:
    env = load_env(path)
    for k, v in env.items():
        os.environ.setdefault(k, v)
    return env


def http_json(url, *, data=None, headers=None, method=None, timeout=30):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            ctype = r.headers.get("Content-Type", "")
            if not body:
                return r.status, None, dict(r.headers)
            if "json" in ctype:
                return r.status, json.loads(body), dict(r.headers)
            return r.status, body[:500].decode("utf-8", "replace"), dict(r.headers)
    except urllib.error.HTTPError as e:
        body = e.read()[:500].decode("utf-8", "replace")
        return e.code, body, dict(e.headers)


class Supabase:
    def __init__(self, env=None):
        env = env or load_env()
        self.url = env["SUPABASE_URL"].rstrip("/")
        self.key = env["SUPABASE_SERVICE_ROLE_KEY"]

    def headers(self, extra=None):
        h = {"apikey": self.key, "Authorization": "Bearer " + self.key,
             "Content-Type": "application/json", "Accept": "application/json"}
        if extra:
            h.update(extra)
        return h

    def count(self, table, flt=""):
        import urllib.request as _u
        req = _u.Request(f"{self.url}/rest/v1/{table}?select=*{flt}",
                         headers=self.headers({"Prefer": "count=exact", "Range": "0-0"}),
                         method="HEAD")
        with _u.urlopen(req, timeout=30) as r:
            return r.headers.get("Content-Range", "")

    def select(self, table, query):
        st, body, _ = http_json(f"{self.url}/rest/v1/{table}?{query}", headers=self.headers())
        return st, body

    def delete(self, table, query):
        st, body, _ = http_json(f"{self.url}/rest/v1/{table}?{query}",
                                headers=self.headers({"Prefer": "return=representation"}),
                                method="DELETE")
        return st, body


class Linear:
    ENDPOINT = "https://api.linear.app/graphql"

    def __init__(self, env=None):
        env = env or load_env()
        self.key = env["LINEAR_API_KEY"]

    def gql(self, query, variables=None):
        payload = json.dumps({"query": query, "variables": variables or {}}).encode()
        st, body, _ = http_json(self.ENDPOINT, data=payload,
                                headers={"Authorization": self.key, "Content-Type": "application/json"})
        if isinstance(body, dict) and body.get("errors"):
            raise RuntimeError(f"Linear GraphQL error: {body['errors']}")
        return st, (body or {}).get("data")

    # -- fixtures ---------------------------------------------------------
    def label_ids(self, names):
        st, data = self.gql("{ issueLabels(first: 100) { nodes { id name } } }")
        by_name = {n["name"]: n["id"] for n in data["issueLabels"]["nodes"]}
        return [by_name[n] for n in names]

    def create_issue(self, title, description, label_names):
        ids = self.label_ids(label_names)
        st, data = self.gql(
            """
            mutation($input: IssueCreateInput!) {
              issueCreate(input: $input) { success issue { id identifier title url state { name } } }
            }
            """,
            {"input": {"teamId": TEAM_ID, "title": title, "description": description,
                       "stateId": STATE_TODO, "labelIds": ids}},
        )
        return data["issueCreate"]["issue"]

    def set_state(self, issue_id, state_id):
        st, data = self.gql(
            """
            mutation($id: String!, $input: IssueUpdateInput!) {
              issueUpdate(id: $id, input: $input) { success issue { id identifier state { name } } }
            }
            """,
            {"id": issue_id, "input": {"stateId": state_id}},
        )
        return data["issueUpdate"]

    def archive_issue(self, issue_id):
        st, data = self.gql(
            """
            mutation($id: String!) { issueArchive(id: $id) { success entity { id identifier archivedAt } } }
            """,
            {"id": issue_id},
        )
        return data["issueArchive"]

    def issue_snapshot(self, issue_id):
        st, data = self.gql(
            """
            query($id: String!) { issue(id: $id) {
              id identifier title url state { name } labels { nodes { name } } archivedAt updatedAt } }
            """,
            {"id": issue_id},
        )
        return data["issue"]
