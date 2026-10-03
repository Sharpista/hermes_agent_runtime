"""Dispatch adapter contracts for the orchestrator runtime chain."""

from __future__ import annotations

import logging
import time
from typing import Callable, Protocol

from .kanban import KanbanDispatcherAdapter, KanbanDispatchStarter, KanbanTaskReader
from .runtime import ExecutionContext, ExecutionResult


class RuntimeDispatchAdapter(Protocol):
    """Callable used by AgentRuntime.execute to run real Hermes work."""

    def __call__(self, context: ExecutionContext) -> ExecutionResult: ...


def kanban_dispatch_adapter(
    start: KanbanDispatchStarter,
    read_task: KanbanTaskReader,
    *,
    poll_seconds: float = 5.0,
    timeout_seconds: float = 3600.0,
    sleep: Callable[[float], None] | None = None,
    monotonic: Callable[[], float] | None = None,
    log: logging.Logger | None = None,
) -> KanbanDispatcherAdapter:
    """Build the versioned Kanban adapter used by the orchestrator chain.

    This helper gives the installed orchestrator a stable import path while the
    implementation remains in ``kanban.py`` for backwards compatibility.
    """

    return KanbanDispatcherAdapter(
        start,
        read_task,
        poll_seconds=poll_seconds,
        timeout_seconds=timeout_seconds,
        sleep=sleep or time.sleep,
        monotonic=monotonic or time.monotonic,
        log=log,
    )


StatusUpdater = Callable[[str, str], None]


__all__ = ["RuntimeDispatchAdapter", "StatusUpdater", "kanban_dispatch_adapter"]
