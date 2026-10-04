"""Session-store variant whose transaction journal lives in shared files.

Unlike the in-memory session targets, this version persists every COMMIT to
a journal file inside a directory shared by both versions under comparison
(``GSB_SHARED_STATE_DIR``).  Running one side of a pair therefore changes
the real on-disk starting conditions observed by the other side unless the
harness restores a frozen preparation snapshot between sides.

Extra commands on top of the base session store:

  0xC0 SEAL  writes an unremovable artifact into the shared directory,
             simulating a side that leaves the preparation state
             unrecoverable after it ran.
"""

from __future__ import annotations

import os
from pathlib import Path

from proto_robust.preparation import UNREMOVABLE_MARKER
from targets import session_store as base

SessionReject = base.SessionReject
frame = base.frame
describe_state = base.describe_state

STATE_DIR_ENV = "GSB_SHARED_STATE_DIR"
JOURNAL_NAME = "journal.log"
SEAL_NAME = "artifact.lock"

_COMMIT = 0x50
_SEAL = 0xC0


def baseline_files() -> dict[str, bytes]:
    """The pristine shared preparation state both sides must start from."""
    return {JOURNAL_NAME: b"GENESIS\n"}


def state_dir() -> Path:
    return Path(os.environ.get(STATE_DIR_ENV, ".shared-state"))


def journal_entries(directory: Path | None = None) -> list[str]:
    path = (directory or state_dir()) / JOURNAL_NAME
    if not path.is_file():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line]


def replay(data: bytes, session: dict | None = None, attempt_namespace: str = "confirm") -> dict:
    if (
        isinstance(data, bytes | bytearray)
        and len(data) >= 3
        and isinstance(session, dict)
    ):
        command = data[0]
        if command == _SEAL:
            # Simulate a run that leaves the shared preparation state
            # unrecoverable: the artifact refuses removal during restore.
            directory = state_dir()
            directory.mkdir(parents=True, exist_ok=True)
            (directory / SEAL_NAME).write_text(UNREMOVABLE_MARKER, encoding="utf-8")
            return session
        if command == _COMMIT and session.get("state") == "selected":
            # Pollute the shared journal *before* the base commit-table
            # defect fires, so a crashing old run still dirties the state.
            directory = state_dir()
            directory.mkdir(parents=True, exist_ok=True)
            with (directory / JOURNAL_NAME).open("a", encoding="utf-8") as handle:
                handle.write(f"COMMIT key={data[1]} ns={attempt_namespace}\n")
    return base.replay(data, session, attempt_namespace)
