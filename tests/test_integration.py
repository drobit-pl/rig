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

from drobit_rig.scale_reader import SCHEMA, ScaleReader
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
