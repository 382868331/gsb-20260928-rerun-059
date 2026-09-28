"""delta 调试式输入缩小：只接受与原样本“同一种失败”的缩小结果。

崩溃和超时绝不互相替代：目标签名包含 kind；REJECTED 永远不算缩小成功。
"""

from __future__ import annotations

from dataclasses import dataclass

from robustlab.outcome import ExecutionResult, FailureSignature
from robustlab.parser.message import _crc8


@dataclass
class MinimizationResult:
    sample: bytes
    signature: FailureSignature
    executions: int


class SameFailureMinimizer:
    def __init__(self, runner) -> None:
        self.runner = runner
        self.executions = 0

    def minimize(self, sample: bytes, target: FailureSignature) -> MinimizationResult:
        current = bytes(sample)
        # 先删 TLV 字段（协议感知），再做字节级 delta-debug 与逐字节简化，
        # 直到一整轮都无法再缩小。
        while True:
            before = current
            current = self._drop_tlvs(current, target)
            current = self._ddmin(current, target)
            current = self._byte_zero(current, target)
            current = self._byte_relax(current, target)
            if current == before:
                break
        return MinimizationResult(current, target, self.executions)

    def _same_failure(self, candidate: bytes, target: FailureSignature) -> ExecutionResult | None:
        result = self.runner.run(candidate)
        self.executions += 1
        sig = result.signature()
        if sig is not None and sig == target:
            return result
        return None

    def _drop_tlvs(self, data: bytes, target: FailureSignature) -> bytes:
        """协议感知缩小：逐个删除 payload 中的 TLV 并重算长度与校验。"""
        parsed = _parse_frame_tlvs(data)
        if parsed is None:
            return data
        current_entries = parsed
        changed = True
        while changed and len(current_entries) > 1:
            changed = False
            for index in range(len(current_entries)):
                trial = current_entries[:index] + current_entries[index + 1 :]
                candidate = _rebuild_frame(data, trial)
                if self._same_failure(candidate, target) is not None:
                    current_entries = trial
                    changed = True
                    break
        return _rebuild_frame(data, current_entries)

    def _ddmin(self, data: bytes, target: FailureSignature) -> bytes:
        current = data
        granularity = 2
        while granularity <= len(current):
            step = max(1, len(current) // granularity)
            progressed = False
            for start in range(0, len(current), step):
                candidate = current[:start] + current[start + step :]
                if self._same_failure(candidate, target) is not None:
                    current = candidate
                    granularity = 2
                    progressed = True
                    break
            if not progressed:
                if granularity >= len(current):
                    break
                granularity = min(len(current), granularity * 2)
        return current

    def _byte_zero(self, data: bytes, target: FailureSignature) -> bytes:
        current = bytearray(data)
        for index in range(len(current)):
            original = current[index]
            if original == 0:
                continue
            current[index] = 0
            if self._same_failure(bytes(current), target) is None:
                current[index] = original
        return bytes(current)

    def _byte_relax(self, data: bytes, target: FailureSignature) -> bytes:
        """尝试把每个字节改成更小的值，便于定位关键字节。"""
        current = bytearray(data)
        for index in range(len(current)):
            original = current[index]
            candidates = (0x01, 0x00)
            if original:
                candidates += (original - 1,)
            for candidate_value in candidates:
                if candidate_value == original:
                    continue
                current[index] = candidate_value
                if self._same_failure(bytes(current), target) is not None:
                    break
            else:
                current[index] = original
        return bytes(current)


def _parse_frame_tlvs(data: bytes) -> list[tuple[int, bytes]] | None:
    if len(data) < 8 or data[0:2] != b"\xa5\x5a":
        return None
    payload_len = (data[4] << 8) | data[5]
    if len(data) != 7 + payload_len + 1:
        return None
    payload = data[7 : 7 + payload_len]
    entries: list[tuple[int, bytes]] = []
    offset = 0
    while offset < len(payload):
        if offset + 2 > len(payload):
            return None
        tag, length = payload[offset], payload[offset + 1]
        value = payload[offset + 2 : offset + 2 + length]
        if len(value) != length:
            return None
        entries.append((tag, value))
        offset += 2 + length
    return entries


def _rebuild_frame(original: bytes, entries: list[tuple[int, bytes]]) -> bytes:
    payload = b"".join(bytes([tag, len(value)]) + value for tag, value in entries)
    length = len(payload)
    header = original[0:4] + bytes([(length >> 8) & 0xFF, length & 0xFF])
    checksum = 0
    for byte in header:
        checksum ^= byte
    return header + bytes([checksum]) + payload + bytes([_crc8(payload)])
