"""Deployment metadata + operator annotations (reference weights, calibration)."""

from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from drobit_rig import session
from drobit_rig.config import RigConfig


def _lock_pointing_at(tmp_path):
    session_dir = tmp_path / "sess"
    session_dir.mkdir()
    (tmp_path / session.LOCK_NAME).write_text(
        json.dumps({"session_dir": str(session_dir), "session_id": 1, "pids": {}})
    )
    return session_dir


def test_cycle_day_from_placement():
    d = datetime.fromisoformat("2026-07-06T12:00:00+02:00")
    assert session._cycle_day("2026-06-01", d) == 35
    assert session._cycle_day("", d) is None
    assert session._cycle_day("not-a-date", d) is None


def test_start_session_records_deployment_and_device(tmp_path, monkeypatch):
    # Don't spawn the real reader/video workers; we only care about meta.json.
    monkeypatch.setattr(session, "_spawn", lambda *a, **k: SimpleNamespace(pid=4321))
    cfg = RigConfig(
        data_root=tmp_path, load_cell_capacity_g=6000, ads1232_gain=128, ads1232_rate_sps=80
    )
    session_dir = session.start_session(
        cfg,
        note="pen A test",
        deployment={
            "breed": "Ross 308",
            "placement_date": "2026-06-01",
            "house": "H1",
            "pen": "P2",
            "bird_count": 20000,
        },
    )
    meta = json.loads((session_dir / "meta.json").read_text())
    dep = meta["deployment"]
    assert dep["breed"] == "Ross 308"
    assert dep["house"] == "H1"
    assert dep["bird_count"] == 20000
    assert isinstance(dep["cycle_day"], int) and dep["cycle_day"] >= 0
    cfg_meta = meta["config"]
    assert cfg_meta["load_cell_capacity_g"] == 6000
    assert cfg_meta["ads1232_gain"] == 128
    assert cfg_meta["ads1232_rate_sps"] == 80


def test_mark_weight_appends_reference(tmp_path):
    session_dir = _lock_pointing_at(tmp_path)
    session.mark_weight(RigConfig(data_root=tmp_path), 1234.5, note="bird A", bird_id="A")
    rec = json.loads((session_dir / "reference_weights.jsonl").read_text().splitlines()[0])
    assert rec["grams"] == 1234.5
    assert rec["bird_id"] == "A"
    assert rec["note"] == "bird A"
    assert "rpi_mono_ns" in rec and "wall_clock" in rec


def test_calibrate_appends_interval(tmp_path):
    session_dir = _lock_pointing_at(tmp_path)
    session.record_calibration(RigConfig(data_root=tmp_path), 0.0, dwell_s=0.0, note="empty")
    rec = json.loads((session_dir / "calibration.jsonl").read_text().splitlines()[0])
    assert rec["grams"] == 0.0
    assert rec["note"] == "empty"
    assert rec["end_mono_ns"] >= rec["start_mono_ns"]


def test_annotations_require_running_session(tmp_path):
    cfg = RigConfig(data_root=tmp_path)  # no lock file
    with pytest.raises(session.SessionError):
        session.mark_weight(cfg, 100.0)
    with pytest.raises(session.SessionError):
        session.record_calibration(cfg, 0.0, dwell_s=0.0)
