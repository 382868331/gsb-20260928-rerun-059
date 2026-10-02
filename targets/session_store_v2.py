"""Revised project-owned session target used for two-version comparison.

session_store_v2 speaks the same frame protocol as session_store but
represents the "new" build under study:

* commit-table fixed: COMMIT with an out-of-range operation key is now an
  explicit, documented SessionReject instead of an IndexError.
* mode-table still over-reads, but the lookup moved into a helper so the
  defect sits at a different file/line than in the old build.  Both builds
  raise IndexError for PREPARE key 0xFE, at *different* locations.
* CONFIG retired: the command is now rejected, so any sequence whose
  session build-up depends on CONFIG cannot finish its preparation here --
  the timing-stall trigger is never reached and the pair is incomparable.
* intermittent fixed: FLAKY no longer raises.
* process-panic unchanged: PANIC still aborts the worker in selected
  state, so both builds show the same behaviour for that sequence.

Explicit malformed input is rejected with SessionReject and is not a
robustness finding.
"""

from __future__ import annotations

import os
import time

from proto_robust.models import ParseReject


class SessionReject(ParseReject):
    """A documented, explicit session-level rejection."""


_OPEN, _ARMED, _SELECTED = "open", "armed", "selected"

_MODES = ("read", "write", "append")
_BUCKETS = ("primary", "mirror", "archive")
_COMMIT_OPS = (b"PUT", b"DEL")

TIMING_STALL_SECONDS = 1.5

_COMMIT = 0x50
_PANIC = 0xB0


def frame(command: int, key: int = 0, payload: bytes = b"") -> bytes:
    return bytes([command, key, len(payload)]) + payload


def describe_state(session: dict) -> str:
    return str(session.get("state", "closed"))


def _lookup_mode(key: int) -> str:
    # Defect site (relocated): key 0xFE still over-reads the mode table, but
    # from this helper -- a different file/line than the old build.
    return _MODES[key]


def replay(data: bytes, session: dict | None = None, attempt_namespace: str = "confirm") -> dict:
    """Apply one frame to the supplied session and return the session dict."""
    if not isinstance(data, bytes | bytearray):
        raise SessionReject("input must be bytes")
    if len(data) < 3:
        raise SessionReject("truncated frame header")
    command, key, length = data[0], data[1], data[2]
    if len(data) != 3 + length:
        raise SessionReject("payload length mismatch")

    if session is None:
        session = {
            "state": "closed",
            "mode": None,
            "bucket": None,
            "stall": False,
            "flaky_calls": 0,
        }
    state = session.setdefault("state", "closed")

    if command == 0x10:  # OPEN
        if state != "closed":
            raise SessionReject("OPEN only valid from closed")
        session["state"] = _OPEN
        return session
    if command == 0x60:  # CLOSE
        session["state"] = "closed"
        session["mode"] = None
        session["bucket"] = None
        session["stall"] = False
        session["flaky_calls"] = 0
        return session
    if state == "closed":
        raise SessionReject("session is not open")

    if command == 0x20:  # PREPARE
        if state != _OPEN:
            raise SessionReject("PREPARE only valid from open")
        if key == 0xFE:
            session["mode"] = _lookup_mode(key)
        if key == 0 or key > len(_MODES):
            raise SessionReject("unknown mode")
        session["mode"] = _MODES[key - 1]
        session["state"] = _ARMED
        return session
    if command == 0x30:  # SELECT
        if state != _ARMED:
            raise SessionReject("SELECT only valid from armed")
        if key == 0 or key > len(_BUCKETS):
            raise SessionReject("unknown bucket")
        session["bucket"] = _BUCKETS[key - 1]
        session["state"] = _SELECTED
        return session
    if command == 0x40:  # SET
        if key == 0:
            raise SessionReject("SET requires a key")
        session.setdefault("values", {})[key] = bytes(data[3:])
        return session
    if command == 0x70:  # CONFIG retired in this build
        raise SessionReject("CONFIG retired in v2; timing stall cannot be armed")
    if command == 0x80:  # TIMING
        if session.get("stall"):
            time.sleep(TIMING_STALL_SECONDS)
        return session
    if command == 0xA0:  # FLAKY fixed in this build
        if state != _ARMED or session.get("mode") != "append":
            raise SessionReject("FLAKY only valid in armed append mode")
        session["flaky_calls"] = session.get("flaky_calls", 0) + 1
        return session
    if command == _PANIC:  # PANIC (unchanged)
        if state != _SELECTED:
            raise SessionReject("PANIC only valid after SELECT")
        os._exit(9)
    if command == _COMMIT:  # COMMIT (fixed)
        if state != _SELECTED:
            raise SessionReject("COMMIT only valid after SELECT")
        operation_index = key - 1
        if operation_index < 0 or operation_index >= len(_COMMIT_OPS):
            # Fixed: out-of-range operation keys are explicitly rejected.
            raise SessionReject("unsupported commit operation")
        session["committed"] = _COMMIT_OPS[operation_index]
        session["state"] = _OPEN
        session["mode"] = None
        session["bucket"] = None
        return session
    raise SessionReject(f"unknown command 0x{command:02x}")
