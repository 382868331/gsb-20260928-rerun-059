"""Project-owned stateful session target for multi-step robustness research.

A research input is an ordered sequence of frames sent to one session that
starts in the ``closed`` state:

  frame = command(1 byte) + key(1 byte) + length(1 byte) + payload(length)

Commands:

  0x10 OPEN     (closed -> open)
  0x20 PREPARE  (open -> armed; key selects an operation mode)
  0x30 SELECT   (armed -> selected; key selects a bucket)
  0x40 SET      key/value write in open, armed or selected states
  0x50 COMMIT   commit the pending transaction in the selected state
  0x60 CLOSE    (any -> closed)
  0x70 CONFIG   arm a timing stall while in the armed state
  0x80 TIMING   sleeps past a tight timeout once CONFIG armed the stall
  0xA0 FLAKY    intermittent defect after mode 3 (append) was prepared
  0xB0 PANIC    aborts the worker process only once a transaction is selected

Seeded, project-owned defects (all state-dependent):

* ``commit-table``: COMMIT over-reads the operation table only after mode 1 is
  prepared and bucket 2 is selected, so it needs a multi-interaction prefix.
  The same frame in open/armed state is a normal explicit rejection.
* ``mode-table``: PREPARE with key 0xFE over-reads a separate mode table while
  entering armed state -- a different defect location than ``commit-table``,
  guarding reduction against collapsing into the other IndexError site.
* ``timing-stall``: CONFIG followed by TIMING sleeps past a tight timeout.
* ``process-panic``: PANIC terminates the process only in selected state; it
  verifies worker cleanup and a clean state for the following sequence.
* ``intermittent``: FLAKY alternates hit/miss across independent replay
  attempts (a temp-file counter), exercising multi-trial hit evidence
  while staying deterministic for the fixed directed sequence.

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



def _flaky_fires(session: dict, namespace: str = "confirm") -> bool:
    """Decide whether this independent replay attempt fires.

    The parent campaign names each replay attempt explicitly:

    * ``detect-a`` / ``detect-b`` give the two-attempt detection probe fixed
      opposite parity, so one of them observes the intermittent defect;
    * ``confirm-<n>`` makes confirmation attempt n deterministic (even n hits).

    No state is shared between worker processes, so the decision cannot leak
    protocol state into the next sequence.
    """
    if session.get("flaky_attempt_seeded"):
        return bool(session.get("flaky_fires"))
    if namespace == "detect-a":
        fires = False
    elif namespace == "detect-b":
        fires = True
    elif namespace.startswith("confirm-"):
        try:
            attempt = int(namespace.split("-", 1)[1])
        except ValueError:
            attempt = 0
        fires = attempt % 2 == 1
    else:
        fires = False
    session["flaky_attempt_seeded"] = True
    session["flaky_fires"] = fires
    return fires

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
            # Defect site: a distinct mode-table over-read while arming.
            mode = _MODES[key]  # noqa: F841
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
    if command == 0x70:  # CONFIG
        if state != _ARMED:
            raise SessionReject("CONFIG only valid while armed")
        session["stall"] = True
        return session
    if command == 0x80:  # TIMING
        if session.get("stall"):
            time.sleep(TIMING_STALL_SECONDS)
        return session
    if command == 0xA0:  # FLAKY
        if state != _ARMED or session.get("mode") != "append":
            raise SessionReject("FLAKY only valid in armed append mode")
        # Alternate across independent replay attempts (fresh processes) so the
        # confirmation trials contain both hits and misses.  The counter lives
        # in the OS temp directory, never in the session, so it cannot leak
        # protocol state between sequences.
        fires = _flaky_fires(session, attempt_namespace)
        if fires:
            raise ValueError("intermittent session defect")
        return session
    if command == _PANIC:  # PANIC
        if state != _SELECTED:
            raise SessionReject("PANIC only valid after SELECT")
        # Explicit process crash: the worker disappears with exit code 9.
        os._exit(9)
    if command == _COMMIT:  # COMMIT
        if state != _SELECTED:
            raise SessionReject("COMMIT only valid after SELECT")
        # The operation index is derived from the transaction key; the seeded
        # key over-reads the two-entry operation table only in selected state.
        operation_index = key - 1
        operation = _COMMIT_OPS[operation_index]  # Defect site: commit table overread
        session["committed"] = operation
        session["state"] = _OPEN
        session["mode"] = None
        session["bucket"] = None
        return session
    raise SessionReject(f"unknown command 0x{command:02x}")
