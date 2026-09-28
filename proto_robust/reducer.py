from __future__ import annotations

from typing import Callable

from proto_robust.models import FailureSignature, Outcome, TargetResult

Predicate = Callable[[bytes], TargetResult]


def reduce_case(data: bytes, signature: FailureSignature, run: Predicate) -> tuple[bytes, int, list[int]]:
    candidate = bytearray(data)
    attempts = 0

    def keeps_failure(value: bytes | bytearray) -> bool:
        nonlocal attempts
        attempts += 1
        result = run(bytes(value))
        return _same_failure(result, signature)

    changed = True
    while changed:
        changed = False
        for position in range(len(candidate) - 1, 0, -1):
            trial = candidate[:position]
            if len(trial) >= 6 and keeps_failure(_repair_sm_length(trial)):
                candidate = bytearray(_repair_sm_length(trial))
                changed = True
                break
        if changed:
            continue
        for position, value in enumerate(candidate):
            if position in (0, 1, 2, 4, 5):
                continue
            for replacement in (0, 1, 2, 4, 255):
                if replacement >= value:
                    continue
                trial = bytearray(candidate)
                trial[position] = replacement
                if keeps_failure(trial):
                    candidate = trial
                    changed = True
                    break
            if changed:
                break
    minimized = bytes(candidate)
    return minimized, attempts, _critical_offsets(minimized, signature, run)


def _critical_offsets(data: bytes, signature: FailureSignature, run: Predicate) -> list[int]:
    offsets: list[int] = []
    for position in range(len(data)):
        trial = bytearray(data)
        trial[position] = 0
        repaired = _repair_sm_length(trial)
        if not _same_failure(run(bytes(repaired)), signature):
            offsets.append(position)
    return offsets


def _repair_sm_length(data: bytes | bytearray) -> bytes:
    if len(data) < 6 or bytes(data[:2]) != b"SM":
        return bytes(data)
    payload_length = len(data) - 6
    return b"SM" + bytes([data[2], data[3]]) + payload_length.to_bytes(2, "big") + bytes(data[6:])


def _same_failure(result: TargetResult, expected: FailureSignature) -> bool:
    if result.outcome != expected.outcome:
        return False
    if expected.outcome is Outcome.CRASH:
        return result.exception_type == expected.exception_type
    if expected.outcome is Outcome.TIMEOUT:
        return True
    if expected.outcome is Outcome.REJECTED:
        return False
    return result.signature.key() == expected.key()
