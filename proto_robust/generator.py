from __future__ import annotations

from dataclasses import dataclass
import random

MAGIC = b"SM"
HEADER_SIZE = 6


@dataclass(frozen=True)
class GeneratedCase:
    index: int
    label: str
    data: bytes


class SimpleMessageGenerator:
    """Deterministic grammar-first generator for the bundled SM protocol."""

    def __init__(self, seed: int) -> None:
        self.seed = seed
        self._rng = random.Random(seed)

    def __iter__(self):
        for index in range(10_000_000):
            yield self.generate(index)

    def generate(self, index: int) -> GeneratedCase:
        if index < 8:
            payload = bytes([index]) if index else b""
            label = "direct-operation"
        elif index < 32:
            # Deterministic sequence covering both same-exception defects
            # and both ARM/FIRE message orderings.
            sequence = (
                (2, 0xFE),  # defect A trigger: operation-table overrun
                (3, 0xFD),  # defect B trigger: length-class overrun
                (7, 0x00),  # ARM
                (9, 0x00),  # FIRE after ARM: correct ordering, succeeds
                (2, 0xFE),  # intervening traffic resets parser state
                (9, 0x00),  # FIRE without ARM: reverse ordering, defect C
                (9, 0x00),  # repeated out-of-order FIRE: intermittent, then crash
                (7, 0x00),  # ARM after FIRE
            )
            flag, probe = sequence[(index - 8) % len(sequence)]
            payload = bytes([probe]) if probe else b""
            framed = MAGIC + bytes([1, flag]) + (1 if probe else 0).to_bytes(2, "big") + payload
            return GeneratedCase(index, "timing-and-defect-sweep", framed)
        elif index < 48:
            flag = (index - 32) % 16
            probe = (0xFE, 0xFD)[(index - 32) // 16 % 2]
            payload = bytes([probe])
            framed = MAGIC + bytes([1, flag]) + (1).to_bytes(2, "big") + payload
            return GeneratedCase(index, "flag-and-probe-sweep", framed)
        elif index < 56:
            payload = bytes([(index - 40) % 256])
            label = "operation-byte-sweep"
        else:
            payload = self._mutated_payload(index)
            label = "seeded-structured-mutation"
        return GeneratedCase(index, label, self._frame(payload))

    def _mutated_payload(self, index: int) -> bytes:
        length = self._rng.randrange(1, 33)
        payload = bytes(self._rng.randrange(256) for _ in range(length))
        choice = index % 5
        if choice == 0:
            payload = bytes([255]) + payload[1:]
        elif choice == 1 and payload:
            position = self._rng.randrange(length)
            payload = payload[:position] + bytes([payload[position] ^ 0xFF]) + payload[position + 1 :]
        elif choice == 2:
            payload = payload[: max(1, length // 2)]
        return payload

    @staticmethod
    def _frame(payload: bytes) -> bytes:
        return MAGIC + bytes([1, 0]) + len(payload).to_bytes(2, "big") + payload
