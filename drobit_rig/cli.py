"""drobit-rig CLI: start / stop / status / events."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import events, session
from .config import RigConfig
from .logging_setup import setup_logging


def _config_from_args(args: argparse.Namespace) -> RigConfig:
    defaults = RigConfig()
    return RigConfig(
        data_root=args.root if args.root is not None else defaults.data_root,
        port=args.port,
        baud=args.baud,
        width=args.width,
        height=args.height,
        fps=args.fps,
        segment_ms=args.segment_ms,
        flush_interval_s=args.flush_interval,
        event_threshold=args.threshold,
        load_cell_capacity_g=args.load_cell_capacity,
        ads1232_gain=args.ads_gain,
        ads1232_rate_sps=args.ads_rate,
    )


def main(argv: list[str] | None = None) -> int:
    defaults = RigConfig()
    ap = argparse.ArgumentParser(
        prog="drobit-rig",
        description="Raw data collection rig: 80 Hz load cell + camera.",
    )
    ap.add_argument("--root", type=Path, default=None,
                    help=f"sessions root (default {defaults.data_root}, "
                         "or $DROBIT_RIG_ROOT)")
    ap.add_argument("--port", default=defaults.port)
    ap.add_argument("--baud", type=int, default=defaults.baud)
    ap.add_argument("--width", type=int, default=defaults.width)
    ap.add_argument("--height", type=int, default=defaults.height)
    ap.add_argument("--fps", type=int, default=defaults.fps)
    ap.add_argument("--segment-ms", type=int, default=defaults.segment_ms)
    ap.add_argument("--flush-interval", type=float, default=defaults.flush_interval_s)
    ap.add_argument("--threshold", type=int, default=defaults.event_threshold,
                    help="event detection threshold in raw ADC counts")
    ap.add_argument("--load-cell-capacity", type=int, default=defaults.load_cell_capacity_g,
                    metavar="G", help="load cell full-scale rating in grams")
    ap.add_argument("--ads-gain", type=int, default=defaults.ads1232_gain)
    ap.add_argument("--ads-rate", type=int, default=defaults.ads1232_rate_sps,
                    metavar="SPS", help="ADS1232 output data rate")

    sub = ap.add_subparsers(dest="command", required=True)
    start_parser = sub.add_parser("start", help="start a new session")
    start_parser.add_argument("--note", default="", help="free-text note for meta.json")
    # Deployment metadata: lets the analysis side compute cycle day / expected
    # bird mass and stratify across the growth cycle.
    start_parser.add_argument("--breed", default="", help="e.g. Ross 308, Cobb 500")
    start_parser.add_argument("--placement-date", default="",
                              help="flock placement date YYYY-MM-DD (day 0)")
    start_parser.add_argument("--house", default="", help="house / barn id")
    start_parser.add_argument("--pen", default="", help="pen / section id")
    start_parser.add_argument("--bird-count", type=int, default=None,
                              help="birds in the pen (density context)")
    sub.add_parser("stop", help="stop the running session")
    sub.add_parser("status", help="show session / disk status")
    events_parser = sub.add_parser(
        "events", help="re-run the event scan on a finished session"
    )
    events_parser.add_argument("session_dir", type=Path)

    mw = sub.add_parser(
        "mark-weight",
        help="log a ground-truth reference weight into the running session",
    )
    mw.add_argument("--grams", type=float, required=True)
    mw.add_argument("--bird-id", default="", help="which bird, if known")
    mw.add_argument("--note", default="")

    cal = sub.add_parser(
        "calibrate",
        help="record a calibration interval (hold known mass steady)",
    )
    cal.add_argument("--grams", type=float, required=True,
                     help="known mass on the plate now (0 = empty)")
    cal.add_argument("--dwell", type=float, default=5.0, help="seconds to hold")
    cal.add_argument("--note", default="")

    args = ap.parse_args(argv)
    setup_logging("cli")
    cfg = _config_from_args(args)

    try:
        if args.command == "start":
            deployment = {
                "breed": args.breed,
                "placement_date": args.placement_date,
                "house": args.house,
                "pen": args.pen,
                "bird_count": args.bird_count,
            }
            session_dir = session.start_session(
                cfg, note=args.note, deployment=deployment
            )
            print(f"started: {session_dir}")
        elif args.command == "stop":
            session_dir = session.stop_session(cfg)
            print(f"stopped: {session_dir}")
        elif args.command == "status":
            print(json.dumps(session.session_status(cfg), indent=2))
        elif args.command == "events":
            n = events.scan_session(args.session_dir, threshold=cfg.event_threshold)
            print(f"{n} events -> {args.session_dir / 'events.jsonl'}")
        elif args.command == "mark-weight":
            path = session.mark_weight(
                cfg, args.grams, note=args.note, bird_id=args.bird_id
            )
            print(f"logged {args.grams} g -> {path}")
        elif args.command == "calibrate":
            print(f"hold {args.grams} g steady for {args.dwell}s…")
            path = session.record_calibration(
                cfg, args.grams, dwell_s=args.dwell, note=args.note
            )
            print(f"calibration interval -> {path}")
    except session.SessionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
