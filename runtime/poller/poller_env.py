"""Server-side secret loading for the permanent runtime poller (LOL-97 / t_a4d0e749).

Secrets live in ``~/.hermes/shared/.env`` (mode 600) and are loaded *into the
process* here -- never expanded on a command line and never written to the
systemd unit, so they cannot leak into ``ps``, journal metadata or the unit file.

Nothing in this module prints secret values.
"""
from __future__ import annotations

import os
from pathlib import Path

DEFAULT_ENV_FILE = Path("/home/alexandre/.hermes/shared/.env")

# The chain needs exactly these three; anything else in the shared file is not
# required by the poller (least privilege on what we assert, not on what is
# loaded -- the file is the project-wide secret store).
REQUIRED_KEYS = ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "LINEAR_API_KEY")


def env_file_path() -> Path:
    return Path(os.environ.get("POLLER_ENV_FILE", str(DEFAULT_ENV_FILE)))


def load_env(path: Path | None = None) -> dict[str, str]:
    """Parse ``KEY=value`` lines; never logs or returns values to callers' logs."""
    path = path or env_file_path()
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def export_into_environ(path: Path | None = None) -> dict[str, str]:
    """Inject the shared secrets into ``os.environ`` (existing values win)."""
    env = load_env(path)
    for key, value in env.items():
        os.environ.setdefault(key, value)
    return env


def missing_required(path: Path | None = None) -> list[str]:
    env = {**load_env(path), **os.environ}
    return [k for k in REQUIRED_KEYS if not env.get(k)]
