"""End-to-end: ScaleReader against the fake ESP over a pty for ~10 s.

Verifies sample counts, gap detection, reset handling, and the parquet
output — the full path a real session takes, minus the camera.
"""

from __future__ import annotations

import json
import threading
import time

import pyarrow.parquet as pq
import pytest

from drobit_rig import protocol
from drobit_rig.scale_reader import SCHEMA, ParquetSink, ScaleReader, mono_ns
from tests.fake_esp import FakeEsp

RUN_S = 10.0
RATE = 80.0
SESSION_ID = 42


@pytest.fixture()
def fake_esp():
    esp = FakeEsp(
        rate_hz=RATE,
        crc_error_every=57,
        gap_every=100,
        gap_len=3,
        reset_after_s=4.0,
    )
    yield esp
    esp.stop()


def test_scale_reader_end_to_end(fake_esp, tmp_path):
    port = fake_esp.start()
    session_dir = tmp_path / "sess"
    session_dir.mkdir()

    reader = ScaleReader(
        port=port,
        baud=115200,  # a pty ignores baud; use a portable standard rate
        session_id=SESSION_ID,
        session_dir=session_dir,
        flush_interval_s=1.0,
        status_interval_s=1.0,
    )
    stop = threading.Event()
    result: list[int] = []
    thread = threading.Thread(target=lambda: result.append(reader.run(stop)))
    thread.start()
    time.sleep(RUN_S)
    stop.set()
    thread.join(timeout=15)
    assert not thread.is_alive(), "reader did not shut down"
    assert result == [0]

    # --- parquet contents -------------------------------------------------
    table = pq.read_table(session_dir / "scale.parquet")
    assert table.schema.equals(SCHEMA)
    n = table.num_rows
    # ~800 emitted in 10 s minus startup latency, CRC-corrupted (~14),
    # injected gaps (~8 * 3) and reset fallout.
    assert 600 <= n <= int(RUN_S * RATE) + 40, f"unexpected sample count {n}"

    sessions = set(table.column("session").to_pylist())
    assert SESSION_ID in sessions, "frames should carry the START session id"
    assert sessions <= {0, SESSION_ID}, "session 0 only from the reset window"

    mono = table.column("rpi_mono_ns").to_pylist()
    assert mono == sorted(mono), "rpi_mono_ns must be monotonic"
    assert all(r > 0 for r in table.column("raw").to_pylist())

    # --- gap + reset detection ---------------------------------------------
    gaps = [json.loads(line) for line in (session_dir / "gaps.jsonl").read_text().splitlines()]
    injected = [g for g in gaps if g["kind"] == "gap" and g["missing"] == 3]
    assert len(injected) >= 4, f"expected injected gaps recorded, got {gaps}"
    resets = [g for g in gaps if g["kind"] == "reset"]
    # One reset from the fake's watchdog; the reader's re-sent START causes
    # a second seq restart on the fake.
    assert len(resets) >= 1
    assert fake_esp.resets_done == 1

    # --- status.json --------------------------------------------------------
    status = json.loads((session_dir / "status.json").read_text())
    assert status["samples"] == n
    assert status["crc_errors"] >= 5, "corrupted frames must be counted"
    assert status["missing_samples"] >= 3 * len(injected)
    assert status["resets"] >= 1
    assert status["rows_written"] == n
    assert status["serial_connected"] is False  # final write after shutdown

    # Multiple flushes happened (1 s interval over a 10 s run).
    assert pq.ParquetFile(session_dir / "scale.parquet").metadata.num_row_groups >= 3


def test_reader_survives_port_absent_then_stop(tmp_path):
    session_dir = tmp_path / "sess"
    session_dir.mkdir()
    reader = ScaleReader(
        port=str(tmp_path / "nonexistent-port"),
        baud=115200,
        session_id=1,
        session_dir=session_dir,
        flush_interval_s=1.0,
    )
    stop = threading.Event()
    result: list[int] = []
    thread = threading.Thread(target=lambda: result.append(reader.run(stop)))
    thread.start()
    time.sleep(1.5)  # let it cycle through open retries
    stop.set()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert result == [0]
    # Parquet file exists and is a valid (empty) file: footer written on close.
    assert pq.read_table(session_dir / "scale.parquet").num_rows == 0


def test_scale_reader_boot_delay_handshake(tmp_path):
    """The initial START is eaten by the boot reset; the reader must retransmit.

    Models the real bug: opening the port pulses DTR/RTS, resetting the ESP,
    which drops the first START while it boots (~2 s) and then announces itself
    with a status frame. A fire-and-forget START would leave the ESP idle
    forever; the handshake loop recovers.
    """
    esp = FakeEsp(rate_hz=RATE, boot_delay_s=2.0)
    port = esp.start()
    session_dir = tmp_path / "sess"
    session_dir.mkdir()

    reader = ScaleReader(
        port=port,
        baud=115200,
        session_id=SESSION_ID,
        session_dir=session_dir,
        flush_interval_s=1.0,
        status_interval_s=1.0,
        # Long relative to the 2 s boot so the *only* things that send START
        # are the initial open and the status-frame trigger — makes the
        # retransmit count deterministic (2) rather than timing-dependent.
        start_retransmit_interval_s=10.0,
    )
    stop = threading.Event()
    result: list[int] = []
    thread = threading.Thread(target=lambda: result.append(reader.run(stop)))
    thread.start()
    try:
        deadline = time.monotonic() + 8.0
        while reader.samples == 0 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert reader.samples > 0, "handshake never completed; stream never started"
        time.sleep(1.0)  # a healthy stream must not trigger more STARTs
    finally:
        stop.set()
        thread.join(timeout=15)
        esp.stop()
    assert not thread.is_alive()
    assert result == [0]

    # --- handshake shape ---------------------------------------------------
    assert esp.starts_discarded_booting >= 1, "initial START should be lost in boot"
    assert esp.status_frames_emitted >= 1, "ESP should announce readiness"
    assert esp.starts_received == 1, "exactly one START should be accepted"
    # Initial START (lost) + one status-triggered retransmit (lands). No
    # periodic retransmits, and none once streaming (point 5).
    assert reader.start_sends == 2, f"unexpected START count {reader.start_sends}"

    # --- no phantom gaps/resets from the handshake -------------------------
    assert reader.resets == 0
    assert reader.gap_events == 0
    gaps = (session_dir / "gaps.jsonl").read_text().splitlines()
    assert gaps == [], f"handshake should produce no gap records, got {gaps}"

    # --- streaming really happened, all on our session ---------------------
    table = pq.read_table(session_dir / "scale.parquet")
    assert table.num_rows == reader.samples > 0
    assert set(table.column("session").to_pylist()) == {SESSION_ID}


def test_scale_reader_handshake_periodic_retransmit(tmp_path):
    """Recovery when the boot status frame is lost too: only the 2 s (here
    0.5 s) periodic retransmit can re-arm the ESP — the robust path that works
    regardless of reset timing.
    """
    esp = FakeEsp(rate_hz=RATE, boot_delay_s=1.5, emit_boot_status=False)
    port = esp.start()
    session_dir = tmp_path / "sess"
    session_dir.mkdir()

    reader = ScaleReader(
        port=port,
        baud=115200,
        session_id=SESSION_ID,
        session_dir=session_dir,
        flush_interval_s=1.0,
        status_interval_s=1.0,
        start_retransmit_interval_s=0.5,
    )
    stop = threading.Event()
    result: list[int] = []
    thread = threading.Thread(target=lambda: result.append(reader.run(stop)))
    thread.start()
    try:
        deadline = time.monotonic() + 8.0
        while reader.samples == 0 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert reader.samples > 0, "periodic retransmit never re-armed the ESP"
        time.sleep(0.8)
    finally:
        stop.set()
        thread.join(timeout=15)
        esp.stop()
    assert not thread.is_alive()
    assert result == [0]

    assert esp.status_frames_emitted == 0, "status was suppressed for this test"
    assert esp.starts_discarded_booting >= 1, "STARTs during boot must be dropped"
    assert esp.starts_received == 1, "exactly one START lands, once boot is done"
    # Several retransmits fire during the 1.5 s boot (initial + ~0.5 s ticks).
    assert reader.start_sends >= 3, f"expected repeated retransmits, got {reader.start_sends}"
    # Once streaming, retransmits stop and nothing looks like a gap/reset.
    assert reader.resets == 0
    assert reader.gap_events == 0
    assert set(pq.read_table(session_dir / "scale.parquet").column("session").to_pylist()) == {
        SESSION_ID
    }


class _RecordingSerial:
    """Minimal Serial stand-in: records writes, ignores flush."""

    def __init__(self) -> None:
        self.writes: list[bytes] = []

    def write(self, data) -> None:
        self.writes.append(bytes(data))

    def flush(self) -> None:
        pass


def test_wrong_session_resends_start_without_counting_gap(tmp_path):
    """A stale-session sample re-arms the ESP but is never counted as a gap."""
    session_dir = tmp_path / "sess"
    session_dir.mkdir()
    reader = ScaleReader(
        port="unused", baud=115200, session_id=SESSION_ID, session_dir=session_dir,
    )
    sink = ParquetSink(session_dir / "scale.parquet")
    ser = _RecordingSerial()

    def frame(session: int, seq: int) -> protocol.Frame:
        return protocol.Frame(protocol.FrameType.SAMPLE, session, seq, seq * 1000, 100_000)

    try:
        with open(session_dir / "gaps.jsonl", "a") as gaps_file:
            # A sample on our session establishes the seq baseline and streaming.
            reader._handle_frame(frame(SESSION_ID, 0), mono_ns(), sink, gaps_file, ser)
            assert reader._started is True
            sends_before = len(ser.writes)

            # A previous run's session is still streaming (forward-jumped seq).
            reader._handle_frame(frame(999, 5000), mono_ns(), sink, gaps_file, ser)
            assert len(ser.writes) == sends_before + 1, "should re-send START"
            assert ser.writes[-1] == protocol.build_start(SESSION_ID)
            assert reader._started is False
            # The stale frame must not pollute gap stats (point 3).
            assert reader.gap_events == 0
            assert reader.missing_samples == 0

            # Our session comes back; no spurious reset, streaming resumes.
            reader._handle_frame(frame(SESSION_ID, 5001), mono_ns(), sink, gaps_file, ser)
            assert reader._started is True
            assert reader.gap_events == 0
    finally:
        sink.close()

    records = [json.loads(line) for line in (session_dir / "gaps.jsonl").read_text().splitlines()]
    assert any(r["kind"] == "wrong_session" and r["got_session"] == 999 for r in records)
    assert all(r["kind"] != "gap" for r in records), f"no gap record expected: {records}"
