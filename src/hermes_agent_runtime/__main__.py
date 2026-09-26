"""Run the coordinator on the Hermes server with an installed dispatch adapter."""

from __future__ import annotations

import argparse
import importlib
import logging
import os
import time

from .linear import LinearClient
from .orchestrator import Orchestrator
from .runtime import AgentRuntime, ExecutionBlocked, RunConflict
from .supabase import SupabaseStore


def main() -> None:
    parser = argparse.ArgumentParser(description="Linear to Hermes coordinator")
    parser.add_argument("--adapter", required=True, help="Python module:function that executes a StageRequest")
    parser.add_argument("--team-id", default=os.getenv("LINEAR_TEAM_ID"))
    parser.add_argument("--issue", help="One Linear issue identifier for a single run")
    parser.add_argument("--interval", type=int, default=30, help="Poll interval in seconds")
    parser.add_argument("--allow-pr-publish", action="store_true", help="Authorize the publish_pr stage for this process")
    args = parser.parse_args()
    if not args.issue and not args.team_id:
        parser.error("--team-id or LINEAR_TEAM_ID is required for polling")
    if args.interval < 5:
        parser.error("--interval must be at least 5 seconds")
    module, separator, function = args.adapter.partition(":")
    if not separator or not module or not function:
        parser.error("--adapter must be module:function")
    dispatch = getattr(importlib.import_module(module), function)
    linear = LinearClient.from_environment()
    coordinator = Orchestrator(linear, AgentRuntime(SupabaseStore.from_environment()), dispatch,
                               allow_pr_publish=args.allow_pr_publish)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    def process(identifier: str) -> None:
        try:
            outcome = coordinator.execute(identifier)
            logging.info("issue=%s outcome=%s", identifier, outcome)
        except (RunConflict, ExecutionBlocked) as exc:
            logging.warning("issue=%s blocked=%s", identifier, type(exc).__name__)
        except Exception as exc:
            logging.error("issue=%s failed=%s", identifier, type(exc).__name__)
            if args.issue:
                raise

    if args.issue:
        process(args.issue)
        return
    while True:
        try:
            for issue in linear.todo_issues(team_id=args.team_id):
                process(issue.identifier)
        except Exception as exc:
            logging.error("Linear polling failed=%s", type(exc).__name__)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
