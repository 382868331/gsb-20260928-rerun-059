"""Fixed session-store version used in differential comparisons."""

from __future__ import annotations

from copy import deepcopy

from proto_robust.models import ParseReject
from targets import session_store as old


SessionReject = old.SessionReject
frame = old.frame
describe_state = old.describe_state


def replay(data: bytes, session: dict | None = None, attempt_namespace: str = "confirm") -> dict:
    if not isinstance(data, bytes | bytearray):
        raise SessionReject("input must be bytes")
    if len(data) < 3:
        raise SessionReject("truncated frame header")
    command, key, length = data[0], data[1], data[2]
    if command == 0x50 and key not in (1, 2):
        prepared_session = deepcopy(session) if isinstance(session, dict) else None
        try:
            old.replay(bytes([command, 1, length]) + data[3:], session, attempt_namespace)
        except ParseReject:
            raise
        finally:
            if prepared_session is not None:
                session.clear()
                session.update(prepared_session)
        raise SessionReject("unknown commit operation")
    return old.replay(data, session, attempt_namespace)
