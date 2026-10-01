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


# --- Stateful session sequence generation ---------------------------------

from proto_robust.sequences import Interaction, SessionSequence  # noqa: E402


def _interaction(name: str, frame_bytes: bytes) -> Interaction:
    return Interaction(name, frame_bytes)


class SessionSequenceGenerator:
    """Deterministic, grammar-first generator of multi-step session inputs.

    Early indexes are directed traces that exercise each seeded state path.
    Later indexes are benign baseline sessions and fixed-seed mutations.
    """

    OPEN = bytes([0x10, 0, 0])
    CLOSE = bytes([0x60, 0, 0])

    def __init__(self, seed: int) -> None:
        self.seed = seed
        self._rng = random.Random(seed)

    def __iter__(self):
        for index in range(10_000_000):
            yield self.generate(index)

    def generate(self, index: int) -> SessionSequence:
        if index == 0:
            return self._case(
                index,
                "directed-commit-table-state-gated",
                [
                    _interaction("open", self.OPEN),
                    _interaction("prepare-mode-1", bytes([0x20, 1, 0])),
                    _interaction("select-bucket-2", bytes([0x30, 2, 0])),
                    _interaction("set-context", bytes([0x40, 5, 1]) + b"A"),
                    _interaction("commit-overread", bytes([0x50, 3, 1]) + b"Z"),
                ],
            )
        if index == 1:
            return self._case(
                index,
                "directed-mode-table-overread",
                [
                    _interaction("open", self.OPEN),
                    _interaction("prepare-bad-mode", bytes([0x20, 0xFE, 0])),
                ],
            )
        if index == 2:
            return self._case(
                index,
                "directed-timing-stall",
                [
                    _interaction("open", self.OPEN),
                    _interaction("prepare-mode-1", bytes([0x20, 1, 0])),
                    _interaction("config-stall", bytes([0x70, 0, 0])),
                    _interaction("timing", bytes([0x80, 0, 0])),
                ],
            )
        if index == 3:
            return self._case(
                index,
                "directed-process-panic-after-select",
                [
                    _interaction("open", self.OPEN),
                    _interaction("prepare-mode-1", bytes([0x20, 1, 0])),
                    _interaction("select-bucket-1", bytes([0x30, 1, 0])),
                    _interaction("panic", bytes([0xB0, 0, 0])),
                ],
            )
        if index == 4:
            return self._case(
                index,
                "directed-intermittent-flaky",
                [
                    _interaction("open", self.OPEN),
                    _interaction("prepare-mode-3", bytes([0x20, 3, 0])),
                    _interaction("flaky-1", bytes([0xA0, 0, 0])),
                    _interaction("flaky-2", bytes([0xA0, 0, 0])),
                ],
            )
        if index < 10:
            return self._case(index, "benign-baseline", self._benign(index))
        return self._case(index, "seeded-session-mutation", self._mutation(index))

    def _case(self, index: int, label: str, steps: list[Interaction]) -> SessionSequence:
        return SessionSequence(index, label, "closed", tuple(steps))

    def _benign(self, index: int) -> list[Interaction]:
        variant = (index - 5) % 5
        steps = [_interaction("open", self.OPEN)]
        if variant == 0:
            steps.append(_interaction("close", self.CLOSE))
        elif variant == 1:
            steps.append(_interaction("prepare-mode-1", bytes([0x20, 1, 0])))
            steps.append(_interaction("close", self.CLOSE))
        elif variant == 2:
            steps.append(_interaction("prepare-mode-2", bytes([0x20, 2, 0])))
            steps.append(_interaction("select-bucket-1", bytes([0x30, 1, 0])))
            steps.append(_interaction("commit-ok", bytes([0x50, 1, 0])))
        elif variant == 3:
            steps.append(_interaction("set", bytes([0x40, 2, 1]) + b"K"))
        else:
            steps.append(_interaction("prepare-mode-3", bytes([0x20, 3, 0])))
            steps.append(_interaction("flaky-ok", bytes([0xA0, 0, 0])))
        return steps

    def _mutation(self, index: int) -> list[Interaction]:
        rng = random.Random(self.seed + index)
        steps = [_interaction("open", self.OPEN)]
        command_choices = (0x20, 0x30, 0x40, 0x50, 0x70, 0x80, 0xA0)
        count = 2 + rng.randrange(0, 4)
        for position in range(count):
            command = command_choices[rng.randrange(len(command_choices))]
            key = rng.randrange(0, 256)
            payload_len = rng.randrange(0, 4)
            payload = bytes(rng.randrange(256) for _ in range(payload_len))
            steps.append(_interaction(f"mut-{position}", bytes([command, key, payload_len]) + payload))
        return steps
