"""Old session-store variant whose crashed commit locks a shared backend.

Both versions of a differential pair talk to one external commit backend,
emulated by a lock file inside ``SESSION_STORE_SHARED_DIR``.  The lock is
scoped to the pair-attempt namespace (the trailing ``-<identity>-<ordinal>``
added by the campaign is stripped), so the two sides of one pair share the
backend while distinct pairs never interfere.

Order effect: when this old version runs first, its commit-table crash
leaves the backend locked and the second side of the same pair cannot
finish preparation -- a preparation failure, not a miss.  When this version
runs second, the backend is still free and the defect reproduces.
"""

from __future__ import annotations

import os
from pathlib import Path
import re

from proto_robust.models import SessionPreparationError
from targets import session_store as base


SessionReject = base.SessionReject
frame = base.frame
describe_state = base.describe_state

_SIDE_SUFFIX = re.compile(r"-[^-]+-\d+$")


def pair_lock_path(attempt_namespace: str) -> Path | None:
    directory = os.environ.get("SESSION_STORE_SHARED_DIR", "")
    if not directory:
        return None
    pair_namespace = _SIDE_SUFFIX.sub("", attempt_namespace)
    return Path(directory) / (pair_namespace + ".lock")


def replay(data: bytes, session: dict | None = None, attempt_namespace: str = "confirm") -> dict:
    if (
        isinstance(data, bytes | bytearray)
        and len(data) >= 2
        and data[0] == 0x50
        and data[1] == 3
        and isinstance(session, dict)
        and session.get("state") == "selected"
    ):
        lock = pair_lock_path(attempt_namespace)
        if lock is not None and lock.exists():
            raise SessionPreparationError("commit backend locked by the other side of this pair")
        try:
            return base.replay(data, session, attempt_namespace)
        except BaseException:
            # The crashing commit leaves the shared backend locked, changing
            # the starting condition of whichever version runs next.
            if lock is not None:
                lock.parent.mkdir(parents=True, exist_ok=True)
                lock.write_text("locked", encoding="utf-8")
            raise
    return base.replay(data, session, attempt_namespace)
