"""Parser edge cases: valid stream, corruption, truncation, ESP reset."""

from __future__ import annotations

import pytest

from drobit_rig.protocol import (
    FRAME_SIZE,
    Frame,
    FrameParser,
    FrameType,
    build_ping,
    build_start,
    build_stop,
    crc8_maxim,
)


def sample(seq: int, session: int = 7, esp_us: int | None = None, raw: int = 123456) -> Frame:
    return Frame(FrameType.SAMPLE, session, seq, esp_us if esp_us is not None else seq * 12500, raw)


def test_crc8_maxim_check_value():
    # Standard check value for CRC-8/MAXIM.
    assert crc8_maxim(b"123456789") == 0xA1
    assert crc8_maxim(b"") == 0


def test_encode_size_and_roundtrip():
    frame = sample(seq=42, raw=-98765)
    wire = frame.encode()
    assert len(wire) == FRAME_SIZE
    parser = FrameParser()
    assert list(parser.feed(wire)) == [frame]
    assert parser.frames_ok == 1
    assert parser.crc_errors == 0


def test_temp_frame_roundtrip():
    frame = Frame(FrameType.TEMP, session=7, seq=5, esp_us=99999, raw=137)
    wire = frame.encode()
    assert len(wire) == FRAME_SIZE
    assert wire[1] == 0x04  # type byte on the wire
    parser = FrameParser()
    assert list(parser.feed(wire)) == [frame]
    assert parser.crc_errors == 0


def test_valid_stream():
    frames = [sample(seq=i) for i in range(50)]
    stream = b"".join(f.encode() for f in frames)
    parser = FrameParser()
    assert list(parser.feed(stream)) == frames
    assert parser.frames_ok == 50
    assert parser.bytes_discarded == 0


def test_payload_containing_sync_bytes():
    # 0xAA bytes inside session/seq/raw must not derail framing.
    frames = [
        Frame(FrameType.SAMPLE, 0xAAAAAAAA, 0xAAAA00AA, 0xAAAAAAAAAAAAAAAA, -1431655766)
        for _ in range(5)
    ]
    parser = FrameParser()
    assert list(parser.feed(b"".join(f.encode() for f in frames))) == frames


def test_corrupted_byte_mid_stream():
    frames = [sample(seq=i) for i in range(3)]
    stream = bytearray(b"".join(f.encode() for f in frames))
    stream[FRAME_SIZE + 10] ^= 0xFF  # corrupt frame 1's esp_us
    parser = FrameParser()
    out = list(parser.feed(bytes(stream)))
    assert out == [frames[0], frames[2]]
    assert parser.crc_errors >= 1


def test_truncated_frame_at_buffer_boundary():
    frames = [sample(seq=i) for i in range(2)]
    stream = b"".join(f.encode() for f in frames)
    parser = FrameParser()
    out = list(parser.feed(stream[:FRAME_SIZE + 9]))  # frame 1 cut mid-payload
    assert out == [frames[0]]
    out = list(parser.feed(stream[FRAME_SIZE + 9:]))
    assert out == [frames[1]]
    assert parser.crc_errors == 0
    assert parser.bytes_discarded == 0


def test_byte_at_a_time_feeding():
    frames = [sample(seq=i) for i in range(4)]
    stream = b"".join(f.encode() for f in frames)
    parser = FrameParser()
    out = []
    for i in range(len(stream)):
        out.extend(parser.feed(stream[i:i + 1]))
    assert out == frames


def test_leading_garbage_then_frames():
    frames = [sample(seq=i) for i in range(3)]
    garbage = b"\x00\x01\x02ESP-ROM:esp32s3\r\n\xaa\xbb"
    parser = FrameParser()
    out = list(parser.feed(garbage + b"".join(f.encode() for f in frames)))
    assert out == frames
    assert parser.bytes_discarded >= len(garbage) - 2  # the stray 0xAA costs a scan


def test_esp_reset_mid_session():
    # Frames seq 100..102 (session 7), then reset garbage, then seq 0..2
    # with session 0: parser must yield all six frames.
    before = [sample(seq=100 + i) for i in range(3)]
    after = [Frame(FrameType.SAMPLE, 0, i, i * 12500, 99) for i in range(3)]
    garbage = b"\xaaJUNK\x00\xff\xfe partial frame \xaa\x01\x02"
    stream = (
        b"".join(f.encode() for f in before)
        + garbage
        + b"".join(f.encode() for f in after)
    )
    parser = FrameParser()
    out = list(parser.feed(stream))
    assert out == before + after
    assert parser.bytes_discarded > 0


def test_pong_and_status_types():
    frames = [
        Frame(FrameType.PONG, 7, 0, 1000, 0),
        Frame(FrameType.STATUS, 7, 1, 2000, 0),
    ]
    parser = FrameParser()
    assert list(parser.feed(b"".join(f.encode() for f in frames))) == frames


def test_unknown_type_with_valid_crc_is_skipped():
    good = sample(seq=0)
    unknown = bytearray(sample(seq=1).encode())
    unknown[1] = 0x7F  # unknown type; fix up CRC so only the type is "wrong"
    unknown[-1] = crc8_maxim(bytes(unknown[1:-1]))
    parser = FrameParser()
    out = list(parser.feed(good.encode() + bytes(unknown) + sample(seq=2).encode()))
    assert [f.seq for f in out] == [0, 2]
    assert parser.unknown_types == 1


def test_command_builders():
    assert build_start(0) == b"START 0\n"
    assert build_start(4294967295) == b"START 4294967295\n"
    assert build_stop() == b"STOP\n"
    assert build_ping() == b"PING\n"
    with pytest.raises(ValueError):
        build_start(-1)
    with pytest.raises(ValueError):
        build_start(2**32)
