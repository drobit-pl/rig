"""Post-session event flagging, for navigation during manual labeling only.

Scans scale.parquet, computes a rolling-ish baseline, and flags intervals
where |raw - baseline| exceeds a threshold. Deliberately dumb: the baseline
is the median of each ~60 s block (a step function), not a true per-sample
rolling median — that is 100x cheaper in pure Python and plenty for "jump to
the next interesting bit" navigation. A bird standing on the scale for most
of a block will drag that block's baseline; do not treat these events as
measurements.

Runs standalone:  python -m drobit_rig.events <session_dir> [--threshold N]
"""

from __future__ import annotations

import argparse
import json
import logging
import statistics
from pathlib import Path

import pyarrow.parquet as pq

from .config import RigConfig
from .logging_setup import setup_logging

_LOG = logging.getLogger("events")


def find_events(
    mono_ns: list[int],
    raw: list[int],
    *,
    threshold: int,
    window_s: float = 60.0,
    merge_gap_s: float = 1.0,
) -> list[dict]:
    if not mono_ns:
        return []
    window_ns = int(window_s * 1e9)
    merge_gap_ns = int(merge_gap_s * 1e9)
    t0 = mono_ns[0]

    # Baseline: median per consecutive window_s block.
    block_values: dict[int, list[int]] = {}
    for t, r in zip(mono_ns, raw):
        block_values.setdefault((t - t0) // window_ns, []).append(r)
    block_median = {b: statistics.median(v) for b, v in block_values.items()}

    # Contiguous runs of |raw - baseline| > threshold.
    runs: list[list[int]] = []  # [start_idx, end_idx] inclusive
    in_run = False
    for i, (t, r) in enumerate(zip(mono_ns, raw)):
        above = abs(r - block_median[(t - t0) // window_ns]) > threshold
        if above and not in_run:
            runs.append([i, i])
            in_run = True
        elif above:
            runs[-1][1] = i
        else:
            in_run = False

    # Merge runs separated by less than merge_gap_s.
    merged: list[list[int]] = []
    for run in runs:
        if merged and mono_ns[run[0]] - mono_ns[merged[-1][1]] < merge_gap_ns:
            merged[-1][1] = run[1]
        else:
            merged.append(run)

    events = []
    for start, end in merged:
        peak_idx = max(
            range(start, end + 1),
            key=lambda i: abs(
                raw[i] - block_median[(mono_ns[i] - t0) // window_ns]
            ),
        )
        events.append({
            "start_mono_ns": mono_ns[start],
            "end_mono_ns": mono_ns[end],
            "peak_raw": raw[peak_idx],
        })
    return events


def scan_session(
    session_dir: Path,
    *,
    threshold: int,
    window_s: float = 60.0,
    merge_gap_s: float = 1.0,
) -> int:
    """Write <session_dir>/events.jsonl; returns the number of events."""
    parquet_path = session_dir / "scale.parquet"
    events_path = session_dir / "events.jsonl"
    if not parquet_path.exists():
        _LOG.warning("no scale.parquet in %s; skipping event scan", session_dir)
        return 0
    table = pq.read_table(parquet_path, columns=["rpi_mono_ns", "raw"])
    events = find_events(
        table.column("rpi_mono_ns").to_pylist(),
        table.column("raw").to_pylist(),
        threshold=threshold,
        window_s=window_s,
        merge_gap_s=merge_gap_s,
    )
    with open(events_path, "w") as f:
        for event in events:
            f.write(json.dumps(event) + "\n")
    _LOG.info("%d events (threshold=%d) -> %s", len(events), threshold, events_path)
    return len(events)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("session_dir", type=Path)
    ap.add_argument("--threshold", type=int, default=RigConfig().event_threshold)
    ap.add_argument("--window", type=float, default=60.0)
    ap.add_argument("--merge-gap", type=float, default=1.0)
    args = ap.parse_args(argv)

    setup_logging("events")
    scan_session(
        args.session_dir,
        threshold=args.threshold,
        window_s=args.window,
        merge_gap_s=args.merge_gap,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
