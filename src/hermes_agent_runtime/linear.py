"""Minimal server-side Linear GraphQL client for the orchestrator."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class LinearIssue:
    id: str
    identifier: str
    title: str
    description: str
    labels: frozenset[str]
    status: str
    team_id: str


class LinearClient:
    ENDPOINT = "https://api.linear.app/graphql"

    def __init__(self, api_key: str, *, timeout: float = 15):
        if not api_key:
            raise ValueError("LINEAR_API_KEY is required")
        self._key = api_key
        self.timeout = timeout

    @classmethod
    def from_environment(cls) -> "LinearClient":
        return cls(os.environ["LINEAR_API_KEY"])

    def _query(self, query: str, variables: dict[str, object]) -> dict:
        request = Request(self.ENDPOINT, data=json.dumps({"query": query, "variables": variables}).encode(),
                          headers={"Authorization": self._key, "Content-Type": "application/json"}, method="POST")
        try:
            with urlopen(request, timeout=self.timeout) as response:
                document = json.load(response)
        except (HTTPError, URLError) as exc:
            # Never log raw responses: Linear descriptions and credentials may be present.
            raise RuntimeError(f"Linear request failed ({type(exc).__name__})") from None
        if document.get("errors"):
            raise RuntimeError("Linear GraphQL request failed")
        return document["data"]

    def todo_issues(self, *, team_id: str, limit: int = 50) -> list[LinearIssue]:
        query = """query($team: String!, $first: Int!, $after: String) {
          issues(filter: {team: {id: {eq: $team}}, state: {name: {eq: "Todo"}}},
                 first: $first, after: $after) {
            nodes { id identifier title description state { name } team { id } labels { nodes { name } } }
            pageInfo { hasNextPage endCursor }
          }
        }"""
        results: list[LinearIssue] = []
        cursor = None
        while len(results) < limit:
            data = self._query(query, {"team": team_id, "first": min(50, limit - len(results)), "after": cursor})["issues"]
            results.extend(self._issue(node) for node in data["nodes"])
            if not data["pageInfo"]["hasNextPage"]:
                break
            cursor = data["pageInfo"]["endCursor"]
        return results

    def issue(self, issue_id: str) -> LinearIssue:
        data = self._query("""query($id: String!) {
          issue(id: $id) { id identifier title description state { name } team { id } labels { nodes { name } } }
        }""", {"id": issue_id})["issue"]
        if data is None:
            raise ValueError("Linear issue not found")
        return self._issue(data)

    def move(self, issue: LinearIssue, status: str) -> None:
        data = self._query("""query($team: String!) {
          team(id: $team) { states { nodes { id name } } }
        }""", {"team": issue.team_id})
        states = {state["name"]: state["id"] for state in data["team"]["states"]["nodes"]}
        if status not in states:
            raise ValueError(f"Linear state {status!r} is unavailable")
        result = self._query("""mutation($id: String!, $state: String!) {
          issueUpdate(id: $id, input: {stateId: $state}) { success }
        }""", {"id": issue.id, "state": states[status]})
        if result["issueUpdate"]["success"] is not True:
            raise RuntimeError("Linear status update failed")

    @staticmethod
    def _issue(node: dict) -> LinearIssue:
        return LinearIssue(node["id"], node["identifier"], node["title"], node.get("description") or "",
                           frozenset(label["name"] for label in node["labels"]["nodes"]),
                           node["state"]["name"], node["team"]["id"])
