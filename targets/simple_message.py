"""Project-owned deterministic message parser used for safe local fuzzing.

Wire format:
  magic: 2 bytes, b"SM"
  version: 1 byte, currently 1
  flags: 1 byte
  payload_length: 2-byte big-endian unsigned integer
  payload: payload_length bytes

The parser deliberately rejects malformed input with ParseReject.  It contains
three seeded, project-owned defects:

* ``operation-table``: first payload byte 0xff indexes the operation table out
  of bounds (IndexError).
* ``channel-table``: first payload byte in 1..4 with second payload byte 0xfe
  indexes a separate channel table out of bounds (also IndexError, but a
  different defect location).
* ``timing-stall``: flags 0x77 sleeps past the normal harness timeout; it is a
  timing-related defect reported as a timeout rather than a crash.

There are intentionally no external targets.
"""

from __future__ import annotations

from proto_robust.models import ParseReject

MAGIC = b"SM"
SUPPORTED_VERSION = 1
_HEADER_SIZE = 6
_OPERATIONS = (
    b"NOOP",
    b"PING",
    b"DATA",
    b"SYNC",
)
_CHANNELS = (
    "alpha",
    "beta",
    "gamma",
)

# The timing defect is intentionally slower than a tight harness timeout but
# fast enough for a normal campaign timeout.
TIMING_STALL_SECONDS = 1.5


def parse_message(data: bytes) -> dict[str, object]:
    if not isinstance(data, bytes | bytearray):
        raise ParseReject("input must be bytes")
    if len(data) < _HEADER_SIZE:
        raise ParseReject("truncated header")
    if bytes(data[:2]) != MAGIC:
        raise ParseReject("bad magic")
    version = data[2]
    if version != SUPPORTED_VERSION:
        raise ParseReject("unsupported version")
    flags = data[3]
    payload_length = int.from_bytes(data[4:6], "big")
    if len(data) != _HEADER_SIZE + payload_length:
        raise ParseReject("rejected payload length mismatch")

    if flags == 0x77:
        import time

        time.sleep(TIMING_STALL_SECONDS)

    payload = bytes(data[_HEADER_SIZE:])
    operation = b""
    channel = ""
    if payload:
        operation_byte = payload[0]
        if operation_byte > 4 and operation_byte != 0xFF:
            raise ParseReject("unknown operation")
        operation = _OPERATIONS[payload[0] - 1]
        if payload_length >= 2 and 1 <= operation_byte <= 4:
            channel = _CHANNELS[payload[1] - 1]
    return {
        "version": version,
        "flags": flags,
        "payload": payload,
        "operation": operation,
        "channel": channel,
    }
