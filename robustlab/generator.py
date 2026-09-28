"""固定种子、可重复的输入生成器。

变异策略对协议半感知：一部分变异保持帧结构（改字段值），
一部分是纯字节级随机变异（模糊边界/长度/校验）。
同一 seed + 同一 iteration 永远产出同一字节串。
"""

from __future__ import annotations

import random
from typing import Iterator

from robustlab.parser.message import MAGIC, _crc8


def build_frame(payload: bytes, version: int = 1, flags: int = 0) -> bytes:
    """构造一个头部校验与 payload CRC 都合法的帧（合法种子输入）。"""
    length = len(payload)
    header = MAGIC + bytes([version, flags, (length >> 8) & 0xFF, length & 0xFF])
    checksum = 0
    for byte in header:
        checksum ^= byte
    return header + bytes([checksum]) + payload + bytes([_crc8(payload)])


def seed_inputs() -> list[bytes]:
    """语料种子：几个结构合法的帧。"""
    return [
        build_frame(bytes([0x01, 2, 0x10, 0x20])),
        build_frame(bytes([0x01, 1, 0x05, 0xDD, 1, 0x01])),  # 合法分组码 1
        build_frame(bytes([0xEE, 1, 0x01])),                 # 奇数间隔，正常
        build_frame(bytes([0x02, 3, 0x41, 0x42, 0x43])),
    ]


MUTATORS = (
    "flip_bit",
    "random_byte",
    "inc_byte",
    "dec_byte",
    "truncate",
    "insert_byte",
    "duplicate_run",
    "swap_bytes",
    "mutate_tlv_value",
    "fix_checksums",
)


class SeededGenerator:
    def __init__(self, seed: int = 20260929) -> None:
        self.seed = seed
        self._rng = random.Random(seed)
        self._corpus = list(seed_inputs())

    def reset(self) -> None:
        self._rng = random.Random(self.seed)
        self._corpus = list(seed_inputs())

    def add_corpus(self, sample: bytes) -> None:
        """覆盖率/新路径样本可以回流到语料（保持可复现：按调用顺序）。"""
        self._corpus.append(bytes(sample))

    def generate(self, index: int) -> bytes:
        """按实验轮次生成：序号完全决定 RNG 推进方式，可重复。"""
        if index < len(self._corpus):
            return bytes(self._corpus[index])
        guided = self._guided_input(index - len(self._corpus))
        if guided is not None:
            return guided
        rng = random.Random((self.seed * 1_000_003 + index) & 0x7FFFFFFF)
        base = bytearray(rng.choice(self._corpus))
        rounds = rng.randint(1, 4)
        for _ in range(rounds):
            base = bytearray(_mutate(bytes(base), rng))
        return bytes(base)

    def _guided_input(self, offset: int) -> bytes | None:
        """确定性的协议边界值用例（固定种子下序号与输入一一对应）。"""
        guided_cases = [
            # group 分组码越界（解析器缺少边界检查的缺陷）
            build_frame(bytes([0xDD, 1, 0x04])),
            build_frame(bytes([0xDD, 1, 0xFF])),
            # keepalive 间隔为偶数 -> 永不退出的缺陷（超时）
            build_frame(bytes([0xEE, 1, 0x02])),
            build_frame(bytes([0xEE, 1, 0x00])),
            # 长度/版本边界（正常应被拒绝，用于区分拒绝与崩溃）
            build_frame(bytes([0x01, 1, 0x00]), version=3),
            build_frame(bytes([0xDD, 1, 0x03])),  # 合法分组码上界
        ]
        if 0 <= offset < len(guided_cases):
            return guided_cases[offset]
        return None

    def stream(self, count: int) -> Iterator[bytes]:
        for index in range(count):
            yield self.generate(index)


def _mutate(data: bytes, rng: random.Random) -> bytes:
    strategy = rng.choice(MUTATORS)
    if not data:
        return bytes([rng.randrange(256)])
    buf = bytearray(data)
    pos = rng.randrange(len(buf))
    if strategy == "flip_bit":
        buf[pos] ^= 1 << rng.randrange(8)
    elif strategy == "random_byte":
        buf[pos] = rng.randrange(256)
    elif strategy == "inc_byte":
        buf[pos] = (buf[pos] + rng.randint(1, 16)) & 0xFF
    elif strategy == "dec_byte":
        buf[pos] = (buf[pos] - rng.randint(1, 16)) & 0xFF
    elif strategy == "truncate":
        cut = rng.randint(1, min(len(buf), 8))
        del buf[max(0, pos - cut) : pos + 1]
    elif strategy == "insert_byte":
        buf.insert(pos, rng.randrange(256))
    elif strategy == "duplicate_run" and len(buf) > 1:
        run = rng.randint(1, min(4, len(buf) - pos))
        buf[pos + run : pos + run] = buf[pos : pos + run]
    elif strategy == "swap_bytes":
        other = rng.randrange(len(buf))
        buf[pos], buf[other] = buf[other], buf[pos]
    elif strategy == "mutate_tlv_value" and len(buf) > 9:
        # 尽量在 payload 区改一个值字节
        value_pos = rng.randrange(8, max(9, len(buf) - 1))
        buf[value_pos] = rng.randrange(256)
    elif strategy == "fix_checksums":
        # 让随机 payload 变异后重新变成“校验合法”的帧，
        # 用来穿透校验逻辑、打到深层处理代码。
        if len(buf) >= 8:
            payload = bytes(buf[7:-1])
            buf[-1] = _crc8(payload)
            checksum = 0
            for byte in buf[0:6]:
                checksum ^= byte
            buf[6] = checksum
    return bytes(buf)
