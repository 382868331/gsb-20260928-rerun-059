"""Target whose required transaction preparation is unavailable."""

from __future__ import annotations

from proto_robust.models import SessionPreparationError
from targets import session_store as base


SessionReject = base.SessionReject
frame = base.frame
describe_state = base.describe_state


def replay(data: bytes, session: dict | None = None, attempt_namespace: str = "confirm") -> dict:
    if (
        isinstance(data, bytes | bytearray)
        and len(data) >= 1
        and data[0] == 0x50
        and isinstance(session, dict)
        and session.get("state") == "selected"
        and len(data) >= 2
        and data[1] == 3
    ):
        raise SessionPreparationError("transaction backend is not ready")
    return base.replay(data, session, attempt_namespace)
