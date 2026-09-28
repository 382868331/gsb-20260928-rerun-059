"""项目自带的“消息协议”解析器（被测目标，不访问任何外部目标）。

帧格式（小端/大端均显式标注）：

``
 0     1     2        3       4       5       6
+-----+-----+--------+-------+-------+-------+------------------+
| MAGIC (0xA5 0x5A)  | ver   | flags | len BE| hdr XOR| payload + CRC8 |
+-----+-----+--------+-------+-------+-------+------------------+
``

payload 是 TLV 序列：``tag(1) | length(1) | value(length)``，
帧尾再附 1 字节 payload CRC8。

本解析器故意保留两个历史缺陷供健壮性研究演示：

1. ``tag=0xDD`` 的 group 分组码直接索引只有 4 项的分组表，
   缺少边界检查 -> IndexError（CRASH）；
2. ``tag=0xEE`` 的 keepalive 循环把终止值写成了 ``0`` 而不是
   ``<=1``，间隔值为偶数时永不退出（TIMEOUT，而非崩溃）。
"""

from __future__ import annotations

MAGIC = b"\xa5\x5a"
SUPPORTED_VERSIONS = {1, 2}


class ParseError(ValueError):
    """解析器对不合法输入的明确拒绝（这不是崩溃）。"""


def _crc8(data: bytes) -> int:
    crc = 0x07
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def _handle_group(values: bytes) -> str:
    groups = ("alpha", "beta", "gamma", "delta")
    # 缺陷：分组码取值范围为 0..255，但分组表只有 4 项，缺少边界检查。
    return groups[values[0]]


def _handle_keepalive(values: bytes) -> int:
    interval = values[0] if values else 1
    ticks = 0
    # 缺陷：终止条件错误。偶数间隔永远满足为真，循环无法退出。
    while interval % 2 != 1:
        ticks += 1
        if ticks > 1_000_000_000:
            break
    return ticks


def parse_frame_header(data: bytes) -> tuple[int, int, int]:
    if len(data) < 7:
        raise ParseError("frame shorter than 7-byte header")
    if data[0:2] != MAGIC:
        raise ParseError("bad magic")
    version = data[2]
    if version not in SUPPORTED_VERSIONS:
        raise ParseError(f"unsupported version {version}")
    flags = data[3]
    payload_len = (data[4] << 8) | data[5]
    expected = 0  # 头部校验：前 6 字节的 XOR
    for byte in data[0:6]:
        expected ^= byte
    if data[6] != expected:
        raise ParseError("header checksum mismatch")
    return version, flags, payload_len


def parse_message(data: bytes) -> dict:
    """解析完整帧；非法输入抛 ParseError，缺陷路径抛 IndexError/卡死。"""
    version, flags, payload_len = parse_frame_header(data)
    if len(data) != 7 + payload_len + 1:
        raise ParseError("declared payload length does not match frame size")
    payload = data[7 : 7 + payload_len]
    if data[7 + payload_len] != _crc8(payload):
        raise ParseError("payload crc8 mismatch")

    fields: list[tuple[int, bytes]] = []
    offset = 0
    while offset < len(payload):
        if offset + 2 > len(payload):
            raise ParseError("truncated TLV header")
        tag, length = payload[offset], payload[offset + 1]
        offset += 2
        if offset + length > len(payload):
            raise ParseError("truncated TLV value")
        fields.append((tag, payload[offset : offset + length]))
        offset += length

    decoded: dict = {"version": version, "flags": flags, "fields": {}}
    for tag, values in fields:
        if tag == 0x01:
            decoded["fields"]["id"] = values
        elif tag == 0xDD:
            if len(values) < 1:
                raise ParseError("group value is empty")
            decoded["fields"]["group"] = _handle_group(values)
        elif tag == 0xEE:
            decoded["fields"]["keepalive_ticks"] = _handle_keepalive(values)
        else:
            decoded["fields"][f"tag_{tag:02x}"] = values
    return decoded
