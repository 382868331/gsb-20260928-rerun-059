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
        elif index == 8:
            return GeneratedCase(index, "directed-operation-table-overread", self._frame(b"\xff"))
        elif index == 9:
            return GeneratedCase(index, "directed-channel-table-overread", self._frame(b"\x01\xfe"))
        elif index == 10:
            return GeneratedCase(
                index,
                "directed-timing-stall",
                MAGIC + bytes([1, 0x77]) + (0).to_bytes(2, "big"),
            )
        elif index < 27:
            payload = bytes([(index - 11) % 256])
            label = "operation-byte-sweep"
        else:
            payload = self._mutated_payload(index - 3)
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
