#!/usr/bin/env python3
"""Stand-in for rpicam-vid so video.py can be tested without a camera.

--help prints a flag list shaped like rpicam-vid's. In run mode it creates a
segment file every --segment ms, appends "# timecode format v2" pts lines if
--save-pts was given, exits 0 on SIGINT.

Behavior knobs via environment (env, not flags, so video.py's command
building stays untouched):

    FAKE_RPICAM_LIBAV=1        advertise libav (mp4) support in --help
    FAKE_RPICAM_DIE_AFTER=N    exit 1 after creating N segments
"""

from __future__ import annotations

import os
import signal
import sys
import time

HELP_TEXT = """\
Valid options are:
  -h [ --help ]         Print this help message
  -t [ --timeout ] arg  Time for which program runs
  -o [ --output ] arg   Set the output file name
  -n [ --nopreview ]    Do not show a preview window
  --width arg           Set the output image width
  --height arg          Set the output image height
  --framerate arg       Set the fixed framerate
  --segment arg         Break the recording into files of this maximum length in ms
  --inline              Force PPS/SPS header with every I frame
  --save-pts arg        Save a timestamp file with this name
  --codec arg           Set the codec to use{libav}
"""


def main() -> int:
    args = sys.argv[1:]
    if "--help" in args or "-h" in args:
        libav = ", h264, mjpeg, yuv420 or libav" if os.environ.get("FAKE_RPICAM_LIBAV") else ""
        print(HELP_TEXT.format(libav=libav))
        return 0

    def get(flag: str, default: str | None = None) -> str | None:
        return args[args.index(flag) + 1] if flag in args else default

    output = get("--output") or get("-o")
    segment_ms = int(get("--segment", "1000"))
    fps = float(get("--framerate", "20"))
    pts_path = get("--save-pts")
    die_after = int(os.environ.get("FAKE_RPICAM_DIE_AFTER", "0"))
    assert output is not None

    stopping = False

    def on_sigint(signum: int, frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, on_sigint)

    pts_file = open(pts_path, "w") if pts_path else None
    if pts_file:
        pts_file.write("# timecode format v2\n")
        pts_file.flush()

    seg = 0
    frame_no = 0
    frame_interval = 1.0 / fps
    start = time.monotonic()
    seg_start_ms: float | None = None

    while not stopping:
        now_ms = (time.monotonic() - start) * 1000.0
        # Same segmentation rule as rpicam's file output: new file at the
        # first frame at/after segment_ms since the current file's first frame.
        if seg_start_ms is None or now_ms - seg_start_ms >= segment_ms:
            if seg_start_ms is not None:
                seg += 1
                if die_after and seg >= die_after:
                    return 1
            seg_start_ms = now_ms
        with open(output % seg, "ab") as f:
            f.write(b"\x00\x00\x00\x01fakeframe")
        if pts_file:
            pts_file.write(f"{now_ms:.3f}\n")
            pts_file.flush()
        frame_no += 1
        time.sleep(frame_interval)

    if pts_file:
        pts_file.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
