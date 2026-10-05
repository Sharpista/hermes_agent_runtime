"""Generate PIN.json for the pinned chain extraction (LOL-97 / t_a4d0e749).

Records the merged commit and a sha256 per file so the read-only canary can prove
the extraction on disk still matches origin/main @ ade1218 without a .git dir.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

CHAIN_ROOT = Path("/home/alexandre/.hermes/runtime/chain/ade1218")
REPO = "/home/alexandre/hermes-agent-runtime"
COMMIT = "ade12189336abf5d28d1c9b1a8ada823577bf834"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    files: dict[str, str] = {}
    for path in sorted(CHAIN_ROOT.rglob("*")):
        if path.is_file() and path.name != "PIN.json":
            files[str(path.relative_to(CHAIN_ROOT))] = sha256(path)

    subject = subprocess.run(
        ["git", "-C", REPO, "log", "-1", "--format=%s", COMMIT],
        capture_output=True, text=True,
    ).stdout.strip()
    remote = subprocess.run(
        ["git", "-C", REPO, "remote", "get-url", "origin"],
        capture_output=True, text=True,
    ).stdout.strip()

    pin = {
        "schema": 1,
        "commit": COMMIT,
        "commit_subject": subject,
        "source_repo": remote,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "files": files,
    }
    (CHAIN_ROOT / "PIN.json").write_text(json.dumps(pin, indent=2) + "\n")
    print(f"wrote {CHAIN_ROOT / 'PIN.json'} commit={COMMIT} files={len(files)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
