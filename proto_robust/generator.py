from __future__ import annotations

from dataclasses import dataclass
import random

from proto_robust.models import SessionSequence, SessionStep

MAGIC = b"SM"
HEADER_SIZE = 6


@dataclass(frozen=True)
class GeneratedCase:
    index: int
    label: str
    sequence: SessionSequence

    @property
    def data(self) -> bytes:
        return self.sequence.flattened()


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
            return self._case(index, label, self._frame(payload))
        elif index == 8:
            return self._case(index, "directed-operation-table-overread", self._frame(b"\xff"))
        elif index == 9:
            return self._case(index, "directed-channel-table-overread", self._frame(b"\x01\xfe"))
        elif index == 10:
            return self._case(
                index,
                "directed-timing-stall",
                MAGIC + bytes([1, 0x77]) + (0).to_bytes(2, "big"),
            )
        elif index == 11:
            arm = self._frame(b"\x01", flags=0x40)
            unrelated = self._frame(b"\x01", flags=0x00)
            trigger = self._frame(b"\x02\x00", flags=0x00)
            return GeneratedCase(
                index,
                "directed-multi-session-state-overread",
                SessionSequence((
                    SessionStep(arm, "enable-guarded-channel-state"),
                    SessionStep(unrelated, "removable-benign-interaction"),
                    SessionStep(trigger, "guarded-channel-trigger"),
                )),
            )
        elif index < 27:
            payload = bytes([(index - 11) % 256])
            label = "operation-byte-sweep"
        else:
            payload = self._mutated_payload(index - 3)
            label = "seeded-structured-mutation"
        return self._case(index, label, self._frame(payload))

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
    def _frame(payload: bytes, flags: int = 0) -> bytes:
        return MAGIC + bytes([1, flags]) + len(payload).to_bytes(2, "big") + payload

    @classmethod
    def _case(cls, index: int, label: str, data: bytes) -> GeneratedCase:
        return GeneratedCase(index, label, SessionSequence.from_bytes(data, label))
