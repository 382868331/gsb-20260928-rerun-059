"""Project-owned parser seeded with three distinct robustness defects.

It reuses the SM wire framing (magic, version, flags, big-endian payload
length) so the existing generator applies unchanged.  The seeded defects are:

* table_index: flags == 2 and payload first byte 0xfe index a 4-entry
  operation table out of bounds -> IndexError at one source location.
* length_index: flags == 3 with payload first byte 0xfd indexes a 2-entry
  length-class table out of bounds -> IndexError at a *different* location.
  Both crashes have the identical exception type and message, so only the
  raise site keeps them in separate findings.
* timing_order: flags == 7 arms a sequence and flags == 9 fires it.
  Sending ARM then FIRE succeeds; FIRE then ARM (the reverse ordering)
  raises IndexError at a third location.  The defect is ordering-dependent
  and, when interleaved with other traffic, reproduces intermittently.

There are intentionally no external targets.
"""

from __future__ import annotations

from proto_robust.models import ParseReject

MAGIC = b"SM"
SUPPORTED_VERSION = 1
_HEADER_SIZE = 6

_OPERATION_TABLE = (b"NOOP", b"PING", b"DATA", b"SYNC")
_LENGTH_CLASSES = ("short", "long")

_FLAG_TABLE_INDEX = 2
_FLAG_LENGTH_INDEX = 3
_FLAG_ARM = 7
_FLAG_FIRE = 9

# Mutable parser state: this is what makes defect C ordering-dependent.
_sequence_armed = False
_parity = 0
_call_parity = 0


def reset_state() -> None:
    global _sequence_armed, _parity, _call_parity
    _sequence_armed = False
    _parity = 0
    _call_parity = 0


def parse_multi(data: bytes) -> dict[str, object]:
    global _sequence_armed, _parity, _call_parity

    if not isinstance(data, bytes | bytearray):
        raise ParseReject("input must be bytes")
    if len(data) < _HEADER_SIZE:
        raise ParseReject("truncated header")
    if bytes(data[:2]) != MAGIC:
        raise ParseReject("bad magic")
    if data[2] != SUPPORTED_VERSION:
        raise ParseReject("unsupported version")
    flags = data[3]
    payload_length = int.from_bytes(data[4:6], "big")
    if len(data) != _HEADER_SIZE + payload_length:
        raise ParseReject("rejected payload length mismatch")
    payload = bytes(data[_HEADER_SIZE:])

    if flags == _FLAG_TABLE_INDEX and payload:
        selector = payload[0]
        if selector not in (0xFE, 0xFD) and selector > 4:
            raise ParseReject("unknown operation")
        # Defect A: 0xfe overruns this four-entry table at this source site.
        index = selector - 0xFA
        if index >= len(_OPERATION_TABLE):
            raise IndexError(f"operation table index {index} out of range")
        operation = _OPERATION_TABLE[index]
        return {"flags": flags, "operation": operation}

    if flags == _FLAG_LENGTH_INDEX and payload:
        selector = payload[0]
        if selector != 0xFD and selector > 2:
            raise ParseReject("unknown length class")
        # Defect B: 0xfd overruns this two-entry table at a distinct site.
        index = selector - 0xFB
        if index >= len(_LENGTH_CLASSES):
            raise IndexError(f"length class index {index} out of range")
        length_class = _LENGTH_CLASSES[index]
        return {"flags": flags, "length_class": length_class}

    if flags == _FLAG_ARM:
        _sequence_armed = True
        _parity = 0
        return {"flags": flags, "armed": True}

    if flags == _FLAG_FIRE:
        if not _sequence_armed:
            # Defect C: firing before arming (reverse ordering) indexes a
            # single-entry state table out of bounds at a third site.
            # Intervening traffic flips call parity, so in a live session
            # the same out-of-order FIRE manifests intermittently.
            state = _LENGTH_CLASSES[:1]
            index = 1 - _call_parity
            _call_parity ^= 1
            if index >= len(state):
                raise IndexError(f"timing state index {index} out of range")
            return {"flags": flags, "state": state[index]}
        _sequence_armed = False
        _call_parity ^= 1
        return {"flags": flags, "fired": True}

    if payload and payload[0] in (0xFE, 0xFD):
        # Intervening traffic flips parity for the timing-dependent defect.
        _parity ^= 1

    _call_parity ^= 1

    return {"flags": flags, "payload": payload}
