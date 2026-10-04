"""Project-owned stateful session target backed by shared on-disk state.

Unlike targets/session_store.py, this target keeps part of its state in a
shared directory (env var SHARED_FILE_STATE_DIR) that every replay process
of a paired group sees:

  ledger.log   one line per committed transaction (this version appends and
               never rolls back, so a commit pollutes the preparation of
               the next side when the pair does not restore in between)
  slots.json   static slot table shipped with the frozen preparation

Frame format matches the session target: command(1) key(1) length(1) payload.

  0x10 OPEN     closed -> open
  0x20 PREPARE  open -> armed (key selects a mode)
  0x30 SELECT   armed -> selected; the bucket table is indexed by the number
                of ledger lines, so a polluted ledger over-reads the table
                (defect ledger-table, IndexError)
  0x40 SET      key/value write in open/armed/selected
  0x50 COMMIT   selected -> closed; key selects a commit operation
                (defect commit-table: key >= 2 over-reads the operation
                table, IndexError); a valid commit appends one ledger line
  0x60 CLOSE    any -> closed
  0x80 TIMING   sleeps past a tight timeout

Explicit malformed input is rejected with SessionReject and is not a
robustness finding.
"""

from __future__ import annotations

import os
from pathlib import Path
import time

from proto_robust.models import ParseReject


class SessionReject(ParseReject):
    """A documented, explicit session-level rejection."""


_OPEN, _ARMED, _SELECTED = "open", "armed", "selected"

_MODES = ("read", "write", "append")
_LEDGER_BUCKETS = ("primary", "mirror", "archive")
_COMMIT_OPS = (b"PUT", b"DEL")

TIMING_STALL_SECONDS = 1.5
SHARED_DIR_ENV = "SHARED_FILE_STATE_DIR"


def frame(command: int, key: int = 0, payload: bytes = b"") -> bytes:
    return bytes([command, key, len(payload)]) + payload


def describe_state(session: dict) -> str:
    return str(session.get("state", "closed"))


def shared_dir() -> Path:
    return Path(os.environ.get(SHARED_DIR_ENV, ".shared_state"))


def ledger_lines() -> list[str]:
    path = shared_dir() / "ledger.log"
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []


def _append_ledger(entry: str) -> None:
    path = shared_dir() / "ledger.log"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(entry + chr(10))


def _select_bucket(session: dict) -> str:
    # Defect ledger-table: the bucket table is indexed by the ledger
    # length, so a ledger polluted by a previous side commit over-reads.
    lines = ledger_lines()
    return _LEDGER_BUCKETS[len(lines)]


def _commit(session: dict, key: int) -> None:
    # Defect commit-table: an unknown commit operation over-reads the
    # operation table instead of being rejected.
    operation = _COMMIT_OPS[key]
    _append_ledger(operation.decode() + ":" + str(session.get("bucket")))


def replay(data: bytes, session: dict | None = None, attempt_namespace: str = "confirm") -> dict:
    if not isinstance(data, bytes | bytearray):
        raise SessionReject("input must be bytes")
    if len(data) < 3:
        raise SessionReject("truncated frame header")
    command, key, length = data[0], data[1], data[2]
    if len(data) < 3 + length:
        raise SessionReject("truncated frame payload")
    if session is None:
        session = {"state": "closed", "mode": None, "bucket": None}
    state = session.get("state", "closed")

    if command == 0x10:
        if state != "closed":
            raise SessionReject("open requires a closed session")
        session["state"] = _OPEN
    elif command == 0x20:
        if state != _OPEN:
            raise SessionReject("prepare requires an open session")
        if key >= len(_MODES):
            raise SessionReject("unknown mode")
        session["mode"] = _MODES[key]
        session["state"] = _ARMED
    elif command == 0x30:
        if state != _ARMED:
            raise SessionReject("select requires an armed session")
        session["bucket"] = _select_bucket(session)
        session["state"] = _SELECTED
    elif command == 0x40:
        if state not in (_OPEN, _ARMED, _SELECTED):
            raise SessionReject("set requires an active session")
        session.setdefault("values", {})[key] = bytes(data[3:3 + length])
    elif command == 0x50:
        if state != _SELECTED:
            raise SessionReject("commit requires a selected transaction")
        _commit(session, key)
        session["state"] = "closed"
    elif command == 0x60:
        session["state"] = "closed"
    elif command == 0x80:
        time.sleep(TIMING_STALL_SECONDS)
    else:
        raise SessionReject("unknown command %#x" % command)
    return session
