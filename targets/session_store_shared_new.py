"""Fixed version of the shared-journal session store.

The fixed version derives its commit-table offset from the shared journal.
When the journal is pristine (only the GENESIS entry), an unknown commit
operation is a clean documented rejection.  When a previous run polluted
the journal, the offset shifts and the lookup over-reads the operation
table -- so without preparation restore, the old->new execution order
changes the new side's result, while the new->old order does not.
"""

from __future__ import annotations

from targets import session_store as base
from targets import session_store_shared as shared

SessionReject = base.SessionReject
frame = base.frame
describe_state = base.describe_state
baseline_files = shared.baseline_files

_COMMIT = 0x50
_COMMIT_OPS = (b"PUT", b"DEL")


def replay(data: bytes, session: dict | None = None, attempt_namespace: str = "confirm") -> dict:
    if (
        isinstance(data, bytes | bytearray)
        and len(data) >= 3
        and data[0] == _COMMIT
        and isinstance(session, dict)
        and session.get("state") == "selected"
    ):
        key = data[1]
        leftover = [entry for entry in shared.journal_entries() if entry != "GENESIS"]
        if leftover:
            # Polluted preparation: the journal offset shifts the lookup
            # past the two-entry operation table.
            operation = _COMMIT_OPS[key + len(leftover)]  # Defect site: polluted journal offset
            session["committed"] = operation
        if key not in (1, 2):
            raise SessionReject("unknown commit operation")
    return shared.replay(data, session, attempt_namespace)
