"""Project-owned deterministic message parser used for safe local fuzzing.

Wire format:
  magic: 2 bytes, b"SM"
  version: 1 byte, currently 1
  flags: 1 byte
  payload_length: 2-byte big-endian unsigned integer
  payload: payload_length bytes

The parser deliberately rejects malformed input with ParseReject.  It contains
one seeded, project-owned defect: a payload whose first byte is 0xff causes an
out-of-bounds table lookup.  There are intentionally no external targets.
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
    payload = bytes(data[_HEADER_SIZE:])
    operation = b""
    if payload:
        operation_byte = payload[0]
        if operation_byte > 4 and operation_byte != 0xFF:
            raise ParseReject("unknown operation")
        operation = _OPERATIONS[payload[0] - 1]
    return {
        "version": version,
        "flags": flags,
        "payload": payload,
        "operation": operation,
    }
