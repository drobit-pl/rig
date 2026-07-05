"""TEMP frames are routed to temperature.jsonl, not scale.parquet."""

from __future__ import annotations

import json

from drobit_rig import protocol
from drobit_rig.scale_reader import ParquetSink, ScaleReader


def test_temp_frame_written_to_jsonl(tmp_path):
    reader = ScaleReader(port="x", baud=1, session_id=7, session_dir=tmp_path)
    sink = ParquetSink(tmp_path / "scale.parquet")
    frame = protocol.Frame(protocol.FrameType.TEMP, 7, 0, 12345, 210)
    with open(tmp_path / "gaps.jsonl", "a") as gaps:
        reader._handle_frame(frame, now_ns=1000, sink=sink, gaps_file=gaps, ser=None)
    sink.close()

    rec = json.loads((tmp_path / "temperature.jsonl").read_text().splitlines()[0])
    assert rec == {"rpi_mono_ns": 1000, "esp_us": 12345, "session": 7, "raw": 210}
    assert reader.temp_frames == 1
    assert reader.samples == 0  # a TEMP frame is not a scale sample
