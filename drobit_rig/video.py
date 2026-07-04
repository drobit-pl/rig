"""rpicam-vid supervisor: segmented H.264 recording + monotonic segment index.

Wraps rpicam-vid as a subprocess (1280x720 @ 20 fps by default, hardware
H.264, --segment). Flags are probed from ``rpicam-vid --help`` at runtime
because rpicam-apps flag names/capabilities differ across builds:

* if the build lists libav support we write .mp4 segments,
* otherwise raw .h264 segments (with --inline so every segment is decodable)
  plus --save-pts timestamps when available.

Timestamping model
------------------
``rec_start_mono_ns`` is CLOCK_MONOTONIC captured right before the subprocess
is spawned. Per-segment start times in video_index.jsonl are:

* source="pts": rec_start_mono_ns + first-frame PTS of the segment, where the
  segment boundaries are derived by replaying rpicam's segmentation rule over
  the --save-pts file. Accurate to ~1 frame relative to the first frame of
  the recording.
* source="estimate": rec_start_mono_ns + seg_index * segment_ms. Additionally
  assumes nominal segment durations, so error grows by up to ~1 frame per
  segment boundary plus camera clock drift over the recording.

Both share one systematic offset: the camera pipeline takes a few hundred ms
to deliver its first frame after spawn, and that latency is not observable
from the outside. Treat absolute video timestamps as good to a few hundred
ms; for tighter alignment put a visible event (LED blink) in frame.

Runs standalone:  python -m drobit_rig.video --session-dir ...
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import IO

from .logging_setup import setup_logging

_LOG = logging.getLogger("video")


def mono_ns() -> int:
    return time.clock_gettime_ns(time.CLOCK_MONOTONIC)


class PtsSegmenter:
    """Replays rpicam-apps' segmentation rule over a --save-pts file.

    The pts file is "# timecode format v2": one millisecond timestamp per
    frame. rpicam's file output starts a new segment at the first frame whose
    PTS is >= segment_ms after the current segment's first frame; replaying
    that rule gives us each segment's first-frame PTS (microseconds).
    """

    def __init__(self, path: Path, segment_ms: int) -> None:
        self._path = path
        self._segment_us = segment_ms * 1000.0
        self._offset = 0
        self._partial = ""
        self._current_start_us: float | None = None
        self.segment_starts_us: list[float] = []

    def poll(self) -> None:
        try:
            with open(self._path) as f:
                f.seek(self._offset)
                chunk = f.read()
                self._offset = f.tell()
        except FileNotFoundError:
            return
        lines = (self._partial + chunk).split("\n")
        self._partial = lines.pop()
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                t_us = float(line) * 1000.0  # file is in milliseconds
            except ValueError:
                continue
            if (
                self._current_start_us is None
                or t_us - self._current_start_us >= self._segment_us
            ):
                self._current_start_us = t_us
                self.segment_starts_us.append(t_us)


class SegmentIndexer:
    """Watches the video dir and appends one line per segment to the index.

    A freshly created segment file may appear before its first frame's PTS is
    flushed to the pts file, so entries wait up to ``pts_grace_s`` for a
    PTS-derived start before falling back to the estimate.
    """

    def __init__(
        self,
        *,
        video_dir: Path,
        prefix: str,
        gen: int,
        rec_start_mono_ns: int,
        fps: int,
        segment_ms: int,
        pts: PtsSegmenter | None,
        index_file: IO[str],
        pts_grace_s: float = 2.0,
    ) -> None:
        self._video_dir = video_dir
        self._gen = gen
        self._rec_start_mono_ns = rec_start_mono_ns
        self._fps = fps
        self._segment_ms = segment_ms
        self._pts = pts
        self._index_file = index_file
        self._pts_grace_s = pts_grace_s
        self._pattern = re.compile(rf"^{re.escape(prefix)}seg_(\d+)\.(?:mp4|h264)$")
        self._pending: dict[int, tuple[str, float]] = {}  # seg -> (name, first seen)
        self._indexed: set[int] = set()

    def poll(self, final: bool = False) -> None:
        if self._pts is not None:
            self._pts.poll()
        for entry in self._video_dir.iterdir():
            match = self._pattern.match(entry.name)
            if not match:
                continue
            seg = int(match.group(1))
            if seg not in self._indexed and seg not in self._pending:
                self._pending[seg] = (entry.name, time.monotonic())
        for seg in sorted(self._pending):
            name, first_seen = self._pending[seg]
            if self._pts is not None and seg < len(self._pts.segment_starts_us):
                start = self._rec_start_mono_ns + int(
                    self._pts.segment_starts_us[seg] * 1000
                )
                source = "pts"
            elif (
                final
                or self._pts is None
                or time.monotonic() - first_seen > self._pts_grace_s
            ):
                start = self._rec_start_mono_ns + seg * self._segment_ms * 1_000_000
                source = "estimate"
            else:
                continue  # give the pts file a moment to catch up
            self._index_file.write(json.dumps({
                "seg": seg,
                "file": name,
                "gen": self._gen,
                "start_mono_ns": start,
                "fps": self._fps,
                "source": source,
            }) + "\n")
            self._index_file.flush()
            self._indexed.add(seg)
            del self._pending[seg]


class VideoRecorder:
    def __init__(
        self,
        *,
        session_dir: Path,
        width: int = 1280,
        height: int = 720,
        fps: int = 20,
        segment_ms: int = 300_000,
        binary: str = "rpicam-vid",
        max_retries: int = 3,
        backoff_s: float = 2.0,
        healthy_run_s: float = 300.0,
        poll_interval_s: float = 0.5,
    ) -> None:
        self._session_dir = session_dir
        self._video_dir = session_dir / "video"
        self._width = width
        self._height = height
        self._fps = fps
        self._segment_ms = segment_ms
        self._binary = binary
        # max_retries counts *consecutive* quick failures; a run longer than
        # healthy_run_s resets the budget so a long session survives more
        # than 3 sporadic camera glitches total.
        self._max_retries = max_retries
        self._backoff_s = backoff_s
        self._healthy_run_s = healthy_run_s
        self._poll_interval_s = poll_interval_s

    # -- probing ------------------------------------------------------------

    def _probe(self) -> tuple[frozenset[str], str]:
        """Return (supported long flags, full help text) from --help."""
        result = subprocess.run(
            [self._binary, "--help"], capture_output=True, text=True, timeout=15
        )
        help_text = result.stdout + result.stderr
        flags = frozenset(re.findall(r"--([A-Za-z0-9][A-Za-z0-9-]*)", help_text))
        return flags, help_text

    def _build_command(
        self, flags: frozenset[str], help_text: str, prefix: str
    ) -> tuple[list[str], Path | None]:
        """Build the rpicam-vid command line; returns (cmd, pts_path or None)."""
        use_mp4 = "libav" in help_text  # libav backend muxes mp4 directly
        ext = "mp4" if use_mp4 else "h264"
        cmd = [
            self._binary,
            "--timeout", "0",  # record until signalled
            "--width", str(self._width),
            "--height", str(self._height),
            "--framerate", str(self._fps),
            "--segment", str(self._segment_ms),
            "--output", str(self._video_dir / f"{prefix}seg_%05d.{ext}"),
        ]
        if "nopreview" in flags:
            cmd.append("--nopreview")
        pts_path: Path | None = None
        if not use_mp4:
            if "inline" in flags:
                cmd.append("--inline")  # SPS/PPS per I-frame: segments decodable
            if "save-pts" in flags:
                # --save-pts is ignored/unsupported with the libav backend,
                # so only used for raw h264 output.
                pts_path = self._video_dir / f"{prefix}pts.txt"
                cmd += ["--save-pts", str(pts_path)]
        return cmd, pts_path

    # -- process management ---------------------------------------------------

    @staticmethod
    def _drain_stderr(proc: subprocess.Popen) -> threading.Thread:
        def _pump() -> None:
            assert proc.stderr is not None
            for line in proc.stderr:
                line = line.rstrip()
                if line:
                    _LOG.info("rpicam-vid: %s", line)

        thread = threading.Thread(target=_pump, name="rpicam-stderr", daemon=True)
        thread.start()
        return thread

    def _shutdown_proc(self, proc: subprocess.Popen) -> None:
        # SIGINT is rpicam-vid's clean-stop path (finalizes libav output);
        # escalate only if it hangs.
        for sig, wait_s in ((signal.SIGINT, 10.0), (signal.SIGTERM, 5.0)):
            if proc.poll() is not None:
                return
            try:
                proc.send_signal(sig)
            except ProcessLookupError:
                return
            try:
                proc.wait(timeout=wait_s)
                return
            except subprocess.TimeoutExpired:
                _LOG.warning("rpicam-vid ignored %s", signal.Signals(sig).name)
        _LOG.error("killing rpicam-vid")
        proc.kill()
        proc.wait(timeout=5.0)

    # -- main loop -----------------------------------------------------------

    def run(self, stop: threading.Event) -> int:
        try:
            flags, help_text = self._probe()
        except FileNotFoundError:
            _LOG.error("%s not found - is rpicam-apps installed?", self._binary)
            return 2
        except subprocess.TimeoutExpired:
            _LOG.error("%s --help timed out", self._binary)
            return 2
        if "segment" not in flags:
            _LOG.error("%s does not support --segment; cannot record", self._binary)
            return 2

        self._video_dir.mkdir(parents=True, exist_ok=True)
        gen = 0  # bumped on restart so the new process never overwrites files
        consecutive_failures = 0

        with open(self._session_dir / "video_index.jsonl", "a") as index_file:
            while not stop.is_set():
                prefix = "" if gen == 0 else f"r{gen}_"
                cmd, pts_path = self._build_command(flags, help_text, prefix)
                _LOG.info("starting: %s", " ".join(cmd))
                rec_start_mono_ns = mono_ns()
                run_started = time.monotonic()
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                self._drain_stderr(proc)
                indexer = SegmentIndexer(
                    video_dir=self._video_dir,
                    prefix=prefix,
                    gen=gen,
                    rec_start_mono_ns=rec_start_mono_ns,
                    fps=self._fps,
                    segment_ms=self._segment_ms,
                    pts=PtsSegmenter(pts_path, self._segment_ms) if pts_path else None,
                    index_file=index_file,
                )

                while proc.poll() is None and not stop.is_set():
                    indexer.poll()
                    stop.wait(self._poll_interval_s)

                if stop.is_set():
                    self._shutdown_proc(proc)
                    indexer.poll(final=True)
                    _LOG.info("recording stopped (rc=%s)", proc.returncode)
                    return 0

                # Died on its own.
                indexer.poll(final=True)
                run_s = time.monotonic() - run_started
                _LOG.error(
                    "rpicam-vid exited unexpectedly rc=%s after %.1fs",
                    proc.returncode, run_s,
                )
                if run_s >= self._healthy_run_s:
                    consecutive_failures = 0
                consecutive_failures += 1
                if consecutive_failures > self._max_retries:
                    _LOG.error(
                        "giving up after %d consecutive failures", self._max_retries
                    )
                    return 1
                delay = self._backoff_s * 2 ** (consecutive_failures - 1)
                gen += 1
                _LOG.warning(
                    "restarting in %.1fs (attempt %d/%d, new prefix r%d_)",
                    delay, consecutive_failures, self._max_retries, gen,
                )
                stop.wait(delay)
        return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--session-dir", type=Path, required=True)
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=720)
    ap.add_argument("--fps", type=int, default=20)
    ap.add_argument("--segment-ms", type=int, default=300_000)
    ap.add_argument("--binary", default="rpicam-vid")
    args = ap.parse_args(argv)

    setup_logging("video", args.session_dir)
    stop = threading.Event()

    def _on_signal(signum: int, _frame: object) -> None:
        _LOG.info("received %s", signal.Signals(signum).name)
        stop.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    recorder = VideoRecorder(
        session_dir=args.session_dir,
        width=args.width,
        height=args.height,
        fps=args.fps,
        segment_ms=args.segment_ms,
        binary=args.binary,
    )
    return recorder.run(stop)


if __name__ == "__main__":
    raise SystemExit(main())
