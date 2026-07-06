"""Binary serial protocol between the ESP32 scale head and the Pi.

Frame layout (little-endian, 23 bytes total):

    offset  size  field    type
    0       1     sync     u8   always 0xAA
    1       1     type     u8   0x01=sample, 0x02=pong, 0x03=status
    2       4     session  u32  session id, set by the START command
    6       4     seq      u32  increments per frame, resets on START
    10      8     esp_us   u64  ESP monotonic microseconds
    18      4     raw      i32  raw ADC value (meaningful for sample frames)
    22      1     crc8     u8   CRC-8/MAXIM over bytes 1..21 (type..raw)

Commands (Pi -> ESP) are ASCII lines: ``START <session_u32>\\n``, ``STOP\\n``,
``PING\\n``.
"""

from __future__ import annotations

import enum
import struct
from dataclasses import dataclass
from typing import Iterator

SYNC = 0xAA
FRAME_SIZE = 23

# sync..raw (22 bytes); the trailing crc8 byte is handled separately.
_STRUCT = struct.Struct("<BBIIQi")
assert _STRUCT.size == FRAME_SIZE - 1

U32_MAX = 0xFFFF_FFFF


class FrameType(enum.IntEnum):
    SAMPLE = 0x01
    PONG = 0x02
    STATUS = 0x03
    TEMP = 0x04
    """Periodic temperature reading; `raw` carries the sensor value (device-
    specific units). Shares the seq space like every other frame; the reader
    routes it to temperature.jsonl instead of scale.parquet."""


_KNOWN_TYPES = frozenset(int(t) for t in FrameType)


def _build_crc_table() -> tuple[int, ...]:
    # CRC-8/MAXIM (Dallas/1-Wire): poly 0x31 reflected -> 0x8C, init 0,
    # no final xor. Check value: crc8_maxim(b"123456789") == 0xA1.
    table = []
    for byte in range(256):
        crc = byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8C if crc & 1 else crc >> 1
        table.append(crc)
    return tuple(table)


_CRC_TABLE = _build_crc_table()


def crc8_maxim(data: bytes | bytearray | memoryview) -> int:
    crc = 0
    for byte in data:
        crc = _CRC_TABLE[crc ^ byte]
    return crc


@dataclass(frozen=True, slots=True)
class Frame:
    type: FrameType
    session: int
    seq: int
    esp_us: int
    raw: int

    def encode(self) -> bytes:
        """Serialize to the 23-byte wire format (used by tests / fake ESP)."""
        body = _STRUCT.pack(SYNC, self.type, self.session, self.seq, self.esp_us, self.raw)
        return body + bytes([crc8_maxim(body[1:])])


class FrameParser:
    """Incremental frame parser: feed bytes, get frames.

    Resyncs on 0xAA so it tolerates garbage (partial frames after an ESP
    reset, line noise). A 0xAA that starts a 23-byte window with a bad CRC is
    skipped one byte at a time, so 0xAA bytes occurring inside payloads do not
    derail parsing for more than one frame.
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self.frames_ok = 0
        self.crc_errors = 0
        self.bytes_discarded = 0
        self.unknown_types = 0

    def feed(self, data: bytes | bytearray) -> Iterator[Frame]:
        self._buf += data
        buf = self._buf
        pos = 0
        try:
            while True:
                sync = buf.find(SYNC, pos)
                if sync < 0:
                    self.bytes_discarded += len(buf) - pos
                    pos = len(buf)
                    return
                self.bytes_discarded += sync - pos
                pos = sync
                if len(buf) - pos < FRAME_SIZE:
                    return  # partial frame at buffer boundary: wait for more
                window = bytes(buf[pos : pos + FRAME_SIZE])
                if crc8_maxim(window[1:22]) != window[22]:
                    self.crc_errors += 1
                    self.bytes_discarded += 1
                    pos += 1
                    continue
                _, ftype, session, seq, esp_us, raw = _STRUCT.unpack(window[:22])
                pos += FRAME_SIZE
                if ftype not in _KNOWN_TYPES:
                    # Valid CRC but a type we don't know: likely a firmware
                    # newer than this reader. Count it, skip the whole frame.
                    self.unknown_types += 1
                    continue
                self.frames_ok += 1
                yield Frame(FrameType(ftype), session, seq, esp_us, raw)
        finally:
            # Compact even if the caller abandons the generator mid-stream.
            del buf[:pos]


def build_start(session_id: int) -> bytes:
    if not 0 <= session_id <= U32_MAX:
        raise ValueError(f"session_id out of u32 range: {session_id}")
    return b"START %d\n" % session_id


def build_stop() -> bytes:
    return b"STOP\n"


def build_ping() -> bytes:
    return b"PING\n"
