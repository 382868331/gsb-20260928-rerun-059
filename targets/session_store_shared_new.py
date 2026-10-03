"""Fixed session-store variant sharing the old version's commit backend.

The unknown commit operation is rejected cleanly, but the fix does not
include backend recovery: if the other side of the pair crashed first and
left the shared backend locked, preparation cannot complete and this
version raises SessionPreparationError instead of producing a result.
"""

from __future__ import annotations

from proto_robust.models import SessionPreparationError
from targets import session_store as base
from targets.session_store_shared import pair_lock_path


SessionReject = base.SessionReject
frame = base.frame
describe_state = base.describe_state


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
        raise SessionReject("unknown commit operation")
    return base.replay(data, session, attempt_namespace)
