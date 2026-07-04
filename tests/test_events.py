"""Event scan: baseline + threshold flagging on synthetic data."""

from __future__ import annotations

import json

import pyarrow as pa
import pyarrow.parquet as pq

from drobit_rig.events import find_events, scan_session
from drobit_rig.scale_reader import SCHEMA

RATE = 80
NS = 1_000_000_000


def make_signal(seconds: float, spikes: list[tuple[float, float, int]]) -> tuple[list[int], list[int]]:
    """Baseline 100k with (start_s, end_s, amplitude) spikes."""
    n = int(seconds * RATE)
    mono = [i * NS // RATE for i in range(n)]
    raw = [100_000] * n
    for start_s, end_s, amp in spikes:
        for i in range(int(start_s * RATE), int(end_s * RATE)):
            raw[i] = 100_000 + amp
    return mono, raw


def test_flat_signal_no_events():
    mono, raw = make_signal(120, [])
    assert find_events(mono, raw, threshold=2000) == []


def test_single_spike_flagged():
    mono, raw = make_signal(120, [(30.0, 33.0, 50_000)])
    events = find_events(mono, raw, threshold=2000)
    assert len(events) == 1
    event = events[0]
    assert abs(event["start_mono_ns"] - 30 * NS) < 2 * NS
    assert abs(event["end_mono_ns"] - 33 * NS) < 2 * NS
    assert event["peak_raw"] == 150_000


def test_nearby_spikes_merged_distant_kept_apart():
    mono, raw = make_signal(
        180,
        [(30.0, 31.0, 50_000), (31.5, 32.5, 50_000), (90.0, 91.0, -40_000)],
    )
    events = find_events(mono, raw, threshold=2000, merge_gap_s=1.0)
    assert len(events) == 2
    assert events[1]["peak_raw"] == 60_000  # negative deviation kept as raw value


def test_subthreshold_noise_ignored():
    mono, raw = make_signal(120, [(10.0, 12.0, 1500)])
    assert find_events(mono, raw, threshold=2000) == []


def test_scan_session_writes_jsonl(tmp_path):
    mono, raw = make_signal(120, [(60.5, 62.0, 30_000)])
    n = len(mono)
    table = pa.table(
        {
            "session": [1] * n,
            "seq": list(range(n)),
            "esp_us": [t // 1000 for t in mono],
            "rpi_mono_ns": mono,
            "raw": raw,
        },
        schema=SCHEMA,
    )
    pq.write_table(table, tmp_path / "scale.parquet")

    assert scan_session(tmp_path, threshold=2000) == 1
    lines = (tmp_path / "events.jsonl").read_text().splitlines()
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert set(event) == {"start_mono_ns", "end_mono_ns", "peak_raw"}
    assert event["peak_raw"] == 130_000


def test_scan_session_missing_parquet(tmp_path):
    assert scan_session(tmp_path, threshold=2000) == 0
