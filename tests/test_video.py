"""VideoRecorder against the fake rpicam-vid (no camera hardware needed)."""

from __future__ import annotations

import json
import shutil
import stat
import sys
import threading
import time
from pathlib import Path

import pytest

from drobit_rig.video import PtsSegmenter, VideoRecorder

FAKE_RPICAM = Path(__file__).parent / "fake_rpicam.py"


@pytest.fixture()
def fake_binary(tmp_path):
    # Copy so we can chmod +x without touching the repo file's mode.
    binary = tmp_path / "fake-rpicam-vid"
    shutil.copy(FAKE_RPICAM, binary)
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    return str(binary)


def run_recorder(recorder: VideoRecorder, run_s: float) -> int:
    stop = threading.Event()
    result: list[int] = []
    thread = threading.Thread(target=lambda: result.append(recorder.run(stop)))
    thread.start()
    time.sleep(run_s)
    stop.set()
    thread.join(timeout=20)
    assert not thread.is_alive(), "recorder did not shut down"
    return result[0]


def read_index(session_dir: Path) -> list[dict]:
    path = session_dir / "video_index.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def make_recorder(session_dir: Path, binary: str, **kwargs) -> VideoRecorder:
    defaults = dict(
        session_dir=session_dir,
        width=640,
        height=480,
        fps=20,
        segment_ms=500,
        binary=binary,
        backoff_s=0.1,
        poll_interval_s=0.1,
    )
    defaults.update(kwargs)
    return VideoRecorder(**defaults)


def test_records_segments_and_index_h264(fake_binary, tmp_path):
    session_dir = tmp_path / "sess"
    session_dir.mkdir()
    recorder = make_recorder(session_dir, fake_binary)
    assert run_recorder(recorder, run_s=2.5) == 0

    segments = sorted((session_dir / "video").glob("seg_*.h264"))
    assert len(segments) >= 2, "fake rpicam should have segmented"
    index = read_index(session_dir)
    assert [e["seg"] for e in index] == list(range(len(index)))
    assert len(index) >= 2
    for entry in index:
        assert entry["fps"] == 20
        assert entry["start_mono_ns"] > 0
        assert entry["source"] in ("pts", "estimate")
    # pts file was advertised and written, so at least seg 0 should be
    # pts-derived rather than estimated.
    assert index[0]["source"] == "pts"
    # Consecutive segment starts must be ~segment_ms apart.
    for a, b in zip(index, index[1:]):
        delta_ms = (b["start_mono_ns"] - a["start_mono_ns"]) / 1e6
        assert 300 < delta_ms < 900


def test_mp4_mode_when_libav_advertised(fake_binary, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_RPICAM_LIBAV", "1")
    session_dir = tmp_path / "sess"
    session_dir.mkdir()
    recorder = make_recorder(session_dir, fake_binary)
    assert run_recorder(recorder, run_s=1.5) == 0

    assert list((session_dir / "video").glob("seg_*.mp4"))
    assert not list((session_dir / "video").glob("*.pts")), "no --save-pts with libav"
    index = read_index(session_dir)
    assert index and all(e["source"] == "estimate" for e in index)


def test_restart_on_death_with_new_prefix(fake_binary, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_RPICAM_DIE_AFTER", "1")  # dies after first segment
    session_dir = tmp_path / "sess"
    session_dir.mkdir()
    recorder = make_recorder(session_dir, fake_binary, max_retries=2)
    rc = run_recorder(recorder, run_s=6.0)
    assert rc == 1, "should give up after max_retries consecutive failures"

    video_dir = session_dir / "video"
    assert list(video_dir.glob("seg_*.h264")), "first run wrote segments"
    assert list(video_dir.glob("r1_seg_*.h264")), "restart used r1_ prefix"
    gens = {e["gen"] for e in read_index(session_dir)}
    assert {0, 1} <= gens


def test_missing_binary_fails_fast(tmp_path):
    session_dir = tmp_path / "sess"
    session_dir.mkdir()
    recorder = make_recorder(session_dir, "/nonexistent/rpicam-vid")
    stop = threading.Event()
    assert recorder.run(stop) == 2


def test_pts_segmenter_replays_boundaries(tmp_path):
    pts = tmp_path / "pts.txt"
    lines = ["# timecode format v2"]
    # 20 fps for 1.3 s: frames at 0, 50, 100 ... 1250 ms; 500 ms segments
    # start at 0, 500, 1000 ms.
    lines += [f"{i * 50.0:.3f}" for i in range(26)]
    pts.write_text("\n".join(lines) + "\n")

    segmenter = PtsSegmenter(pts, segment_ms=500)
    segmenter.poll()
    assert segmenter.segment_starts_us == [0.0, 500_000.0, 1_000_000.0]


def test_pts_segmenter_incremental_reads(tmp_path):
    pts = tmp_path / "pts.txt"
    segmenter = PtsSegmenter(pts, segment_ms=500)
    segmenter.poll()  # file does not exist yet
    assert segmenter.segment_starts_us == []

    with open(pts, "w") as f:
        f.write("# timecode format v2\n0.000\n50.0")
        f.flush()
        segmenter.poll()
        assert segmenter.segment_starts_us == [0.0]
        f.write("00\n510.000\n")  # completes "50.000", then a new segment
        f.flush()
        segmenter.poll()
        assert segmenter.segment_starts_us == [0.0, 510_000.0]
