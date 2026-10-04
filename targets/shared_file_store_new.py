"""Fixed shared-file-store version used in paired replay comparisons.

Fixes relative to targets/shared_file_store.py:

* commit-table: an unknown commit operation is explicitly rejected
  instead of over-reading the operation table.
* ledger pollution: a commit stages its record inside the session and
  never touches the shared ledger, so running this version does not
  change the shared preparation observed by the other side of a pair.

The ledger-table SELECT defect is intentionally kept so that a pair
*without* preparation restore still shows the old side's pollution.
"""

from __future__ import annotations

from targets import shared_file_store as old

SessionReject = old.SessionReject
frame = old.frame
describe_state = old.describe_state


def replay(data: bytes, session: dict | None = None, attempt_namespace: str = "confirm") -> dict:
    if not isinstance(data, bytes | bytearray):
        raise SessionReject("input must be bytes")
    if len(data) < 3:
        raise SessionReject("truncated frame header")
    command, key = data[0], data[1]
    if command == 0x50 and isinstance(session, dict) and session.get("state") == "selected":
        if key >= len(old._COMMIT_OPS):
            raise SessionReject("unknown commit operation")
        operation = old._COMMIT_OPS[key]
        session.setdefault("staged_commits", []).append(
            operation.decode() + ":" + str(session.get("bucket"))
        )
        session["state"] = "closed"
        return session
    return old.replay(data, session, attempt_namespace)
