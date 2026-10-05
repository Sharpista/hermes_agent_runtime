"""Dispatch guard for the runtime poller tick (LOL-102 correction / card t_50a168bb).

The LOL-102 activation showed that a *full* production tick would dispatch the
whole Linear ``Todo`` backlog at once, duplicating cards that already exist on the
board and auto-routing sensitive work (``risk:high`` + ``execution:human`` +
``env:production``). This module is the minimal, auditable fix: it bounds a tick
and makes it idempotent before any ``AgentRuntime.execute`` / ``create_task``.

Three guards, in order, applied to the issues a tick just read:

1. **dedup** - an issue that already has a non-archived Hermes card is skipped;
   the existing card id is reported, nothing is created.
2. **policy** - never dispatch ``risk:high``/``risk:critical``, ``execution:human``
   /``execution:blocked``, ``env:production``, or an issue missing/invalidating the
   mandatory gate labels (``agent:*``/``risk:*``/``execution:*``/``env:*``).
3. **bound** - at most ``POLLER_MAX_DISPATCH_PER_TICK`` issues per tick (default 1);
   the remainder are skipped with reason ``bound`` and picked up by the next tick.

The functions are intentionally pure (no Hermes, Linear, network or SQLite import)
so the whole policy is unit-testable with fakes and read-only. The board/Linear
access lives in ``poller_tick``; this module only decides.

Nothing here prints secrets, mutates the board, or dispatches work.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

#: Default tick bound. 1 is the conservative value requested by the LOL-102
#: correction ("limitar o tick"): one card per tick, raised explicitly via env.
DEFAULT_MAX_DISPATCH_PER_TICK = 1

#: Env var that makes the bound configurable per tick without editing code.
BOUND_ENV_VAR = "POLLER_MAX_DISPATCH_PER_TICK"

#: Canonical agent labels. Mirrors ``hermes_agent_runtime.runtime.AGENTS``; the
#: contract test asserts equality so the policy cannot drift from the chain.
KNOWN_AGENTS: frozenset[str] = frozenset({
    "agent:orchestrator",
    "agent:backend",
    "agent:frontend",
    "agent:devops",
    "agent:qa",
    "agent:review",
    "agent:github",
})

_KNOWN_RISK = frozenset({"low", "medium", "high", "critical"})
_KNOWN_EXECUTION = frozenset({"auto", "human", "blocked"})
_KNOWN_ENV = frozenset({"local", "preview", "staging", "production"})

_AGENT_PREFIX = "agent:"
_RISK_PREFIX = "risk:"
_EXECUTION_PREFIX = "execution:"
_ENV_PREFIX = "env:"


@dataclass(frozen=True)
class Skip:
    """One candidate the tick deliberately did not dispatch."""

    identifier: str
    reason: str


@dataclass(frozen=True)
class GuardOutcome:
    """Result of applying dedup + policy + bound to a tick's candidates."""

    dispatched: tuple[object, ...]
    skipped: tuple[Skip, ...]


def max_dispatch_per_tick(env: Mapping[str, str] | None = None) -> int:
    """Resolve the per-tick bound from ``POLLER_MAX_DISPATCH_PER_TICK``.

    Defaults to :data:`DEFAULT_MAX_DISPATCH_PER_TICK`. Raises ``ValueError`` for a
    present-but-invalid value so a misconfiguration is visible rather than silent;
    the caller (``poller_tick``) falls back to the default with a log line.
    """
    env = os.environ if env is None else env
    raw = env.get(BOUND_ENV_VAR)
    if raw is None or not raw.strip():
        return DEFAULT_MAX_DISPATCH_PER_TICK
    try:
        value = int(raw.strip())
    except ValueError:
        raise ValueError(f"{BOUND_ENV_VAR}={raw!r} is not an integer") from None
    if value < 1:
        raise ValueError(f"{BOUND_ENV_VAR}={value} must be >= 1")
    return value


def _one_label(labels: frozenset[str], prefix: str) -> tuple[str | None, str | None]:
    """Return ``(value, block_reason)`` for exactly-one ``prefix`` label.

    Missing or ambiguous gate labels are "gate ausente" / "gate ambiguo" - both
    block dispatch, so a half-labelled issue never reaches a worker.
    """
    matches = [label[len(prefix):] for label in labels if label.startswith(prefix)]
    if not matches:
        return None, f"gate ausente: rotulo {prefix}* obrigatorio"
    if len(matches) > 1:
        return None, f"gate ambiguo: multiplos rotulos {prefix}*"
    return matches[0], None


def gate_block_reason(identifier: str, labels: Iterable[str]) -> str | None:
    """Return the policy reason to block ``identifier``, or ``None`` if eligible.

    Reason strings are static and label-only: they never include issue titles,
    descriptions, comments or secrets.
    """
    labels = frozenset(labels)

    agents = {label for label in labels if label.startswith(_AGENT_PREFIX)}
    unknown_agents = agents - KNOWN_AGENTS
    if unknown_agents:
        return f"gate invalido: rotulo(s) {sorted(unknown_agents)}"
    if len(agents) != 1:
        return "gate ausente: exatamente um rotulo agent:* obrigatorio"

    risk, reason = _one_label(labels, _RISK_PREFIX)
    if reason:
        return reason
    if risk not in _KNOWN_RISK:
        return f"gate invalido: risk:{risk}"
    if risk in {"high", "critical"}:
        return f"risco {risk} exige humano"

    mode, reason = _one_label(labels, _EXECUTION_PREFIX)
    if reason:
        return reason
    if mode not in _KNOWN_EXECUTION:
        return f"gate invalido: execution:{mode}"
    if mode != "auto":
        return f"execution:{mode} exige humano"

    environment, reason = _one_label(labels, _ENV_PREFIX)
    if reason:
        return reason
    if environment not in _KNOWN_ENV:
        return f"gate invalido: env:{environment}"
    if environment == "production":
        return "env:production exige humano"

    return None


def _identifier_pattern(identifier: str) -> re.Pattern[str]:
    """Word-boundary match for a Linear identifier (``LOL-90`` != ``LOL-900``)."""
    return re.compile(r"(?<![0-9A-Za-z-])" + re.escape(identifier) + r"(?![0-9A-Za-z-])")


def match_existing_cards(tasks: Iterable[object], identifiers: Iterable[str]) -> dict[str, str]:
    """Map ``identifier -> existing card id`` for non-archived board tasks.

    A task counts as "existing" when its title or body references the identifier
    as a token; this catches both cards the poller created (``[runtime] LOL-90``)
    and pre-staged cards that only mention the issue in the body. Archived tasks
    are ignored -- archiving is the explicit way to allow a fresh dispatch.
    """
    wanted = {identifier: _identifier_pattern(identifier) for identifier in identifiers}
    found: dict[str, str] = {}
    for task in sorted(tasks, key=lambda t: str(getattr(t, "id", ""))):
        status = getattr(task, "status", None)
        if status == "archived":
            continue
        title = getattr(task, "title", None) or ""
        body = getattr(task, "body", None) or ""
        for identifier, pattern in wanted.items():
            if identifier in found:
                continue
            if pattern.search(title) or pattern.search(body):
                found[identifier] = str(getattr(task, "id", ""))
    return found


def select_with_guard(
    snapshots: Sequence[object],
    *,
    existing_ids: Iterable[str] = (),
    max_dispatch: int = DEFAULT_MAX_DISPATCH_PER_TICK,
) -> GuardOutcome:
    """Apply dedup, then policy, then the per-tick bound, preserving order.

    ``snapshots`` are objects with ``.identifier`` and ``.labels`` (normally
    ``hermes_agent_runtime.LinearIssueSnapshot``). Returns the snapshots to
    dispatch (already bounded) and the skipped ones with a static reason.
    """
    if max_dispatch < 1:
        raise ValueError("max_dispatch must be >= 1")

    existing = set(existing_ids)
    dispatched: list[object] = []
    skipped: list[Skip] = []
    for snapshot in snapshots:
        identifier = snapshot.identifier
        if identifier in existing:
            skipped.append(Skip(identifier, "card existente no board"))
            continue
        reason = gate_block_reason(identifier, snapshot.labels)
        if reason is not None:
            skipped.append(Skip(identifier, reason))
            continue
        dispatched.append(snapshot)

    bounded = tuple(dispatched[:max_dispatch])
    for snapshot in dispatched[max_dispatch:]:
        skipped.append(Skip(snapshot.identifier, f"bound do tick (max={max_dispatch})"))
    return GuardOutcome(dispatched=bounded, skipped=tuple(skipped))


__all__ = [
    "BOUND_ENV_VAR",
    "DEFAULT_MAX_DISPATCH_PER_TICK",
    "KNOWN_AGENTS",
    "GuardOutcome",
    "Skip",
    "gate_block_reason",
    "match_existing_cards",
    "max_dispatch_per_tick",
    "select_with_guard",
]
