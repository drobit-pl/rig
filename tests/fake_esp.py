"""Fake ESP32 scale head speaking the drobit-rig serial protocol over a pty.

Emits sample frames at a fixed rate once it receives START, answers PING with
a pong frame, and can inject faults:

* CRC errors  (--crc-error-every N: corrupt every Nth frame's CRC byte)
* seq gaps    (--gap-every N: skip --gap-len seq values every Nth frame)
* mid-stream reset (--reset-after S: emit garbage, restart with seq=0 and
  session=0, keep streaming — models firmware that resumes after a watchdog)

Usable as a library (integration tests) or standalone for desk testing:

    python -m tests.fake_esp --reset-after 30
    # prints the pty path; point scale_reader/drobit-rig at it
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import os
import pty
import threading
import time
import tty
from typing import Callable

from drobit_rig.protocol import Frame, FrameType

GARBAGE_ON_RESET = bytes([0x00, 0xAA, 0x13, 0x37, 0xFF, 0xAA, 0x01, 0x02, 0x03])


def _default_raw(seq: int) -> int:
    # Flat baseline with a deterministic wiggle; enough to look alive.
    return 100_000 + (seq % 7) * 3


class FakeEsp:
    def __init__(
        self,
        *,
        rate_hz: float = 80.0,
        crc_error_every: int = 0,
        gap_every: int = 0,
        gap_len: int = 3,
        reset_after_s: float | None = None,
        raw_fn: Callable[[int], int] = _default_raw,
    ) -> None:
        self._rate_hz = rate_hz
        self._crc_error_every = crc_error_every
        self._gap_every = gap_every
        self._gap_len = gap_len
        self._reset_after_s = reset_after_s
        self._raw_fn = raw_fn

        self._master_fd = -1
        self._slave_fd = -1
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

        self.frames_emitted = 0
        self.crc_corrupted = 0
        self.gaps_injected = 0
        self.resets_done = 0

    def start(self) -> str:
        """Open the pty and start the emitter thread; returns the slave path."""
        self._master_fd, self._slave_fd = pty.openpty()
        # Kill echo/canonical mode before anyone attaches, or the kernel
        # would echo our frames back at us as "commands".
        tty.setraw(self._slave_fd)
        flags = fcntl.fcntl(self._master_fd, fcntl.F_GETFL)
        fcntl.fcntl(self._master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        self._thread = threading.Thread(target=self._run, name="fake-esp", daemon=True)
        self._thread.start()
        return os.ttyname(self._slave_fd)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        for fd in (self._master_fd, self._slave_fd):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass

    # -- internals ---------------------------------------------------------

    def _read_commands(self, buf: bytearray) -> list[str]:
        try:
            buf += os.read(self._master_fd, 4096)
        except BlockingIOError:
            pass
        except OSError as exc:
            if exc.errno != errno.EIO:  # EIO: other end closed, fine
                raise
        lines = []
        while b"\n" in buf:
            line, _, rest = bytes(buf).partition(b"\n")
            buf[:] = rest
            lines.append(line.decode("ascii", "replace").strip())
        return lines

    def _write(self, data: bytes) -> None:
        try:
            os.write(self._master_fd, data)
        except (BlockingIOError, OSError):
            pass  # reader not keeping up / gone; a real UART would drop too

    def _emit(self, ftype: FrameType, session: int, seq: int, esp_us: int, raw: int,
              corrupt_crc: bool = False) -> None:
        data = bytearray(Frame(ftype, session, seq, esp_us, raw).encode())
        if corrupt_crc:
            data[-1] ^= 0xFF
        self._write(bytes(data))
        self.frames_emitted += 1

    def _run(self) -> None:
        session = 0
        seq = 0
        streaming = False
        cmd_buf = bytearray()
        t0 = time.monotonic()
        next_frame_at = time.monotonic()
        reset_at = (
            time.monotonic() + self._reset_after_s
            if self._reset_after_s is not None else None
        )

        while not self._stop.is_set():
            for cmd in self._read_commands(cmd_buf):
                if cmd.startswith("START "):
                    try:
                        session = int(cmd.split()[1])
                    except (IndexError, ValueError):
                        continue
                    seq = 0
                    streaming = True
                    next_frame_at = time.monotonic()
                elif cmd == "STOP":
                    streaming = False
                elif cmd == "PING":
                    esp_us = int((time.monotonic() - t0) * 1e6)
                    self._emit(FrameType.PONG, session, seq, esp_us, 0)
                    seq += 1

            if reset_at is not None and time.monotonic() >= reset_at:
                # Watchdog "reboot": spew garbage, lose session, seq restarts.
                self._write(GARBAGE_ON_RESET)
                session = 0
                seq = 0
                self.resets_done += 1
                reset_at = None

            if not streaming:
                time.sleep(0.005)
                continue

            now = time.monotonic()
            if now < next_frame_at:
                time.sleep(min(next_frame_at - now, 0.005))
                continue
            next_frame_at += 1.0 / self._rate_hz

            n = self.frames_emitted + 1
            corrupt = self._crc_error_every > 0 and n % self._crc_error_every == 0
            if corrupt:
                self.crc_corrupted += 1
            esp_us = int((time.monotonic() - t0) * 1e6)
            self._emit(
                FrameType.SAMPLE, session, seq, esp_us, self._raw_fn(seq),
                corrupt_crc=corrupt,
            )
            seq += 1
            if self._gap_every > 0 and n % self._gap_every == 0:
                seq += self._gap_len
                self.gaps_injected += 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rate", type=float, default=80.0)
    ap.add_argument("--crc-error-every", type=int, default=0)
    ap.add_argument("--gap-every", type=int, default=0)
    ap.add_argument("--gap-len", type=int, default=3)
    ap.add_argument("--reset-after", type=float, default=None)
    args = ap.parse_args(argv)

    esp = FakeEsp(
        rate_hz=args.rate,
        crc_error_every=args.crc_error_every,
        gap_every=args.gap_every,
        gap_len=args.gap_len,
        reset_after_s=args.reset_after,
    )
    path = esp.start()
    print(f"fake ESP on {path}  (ctrl-c to quit)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        esp.stop()
        print(
            f"emitted {esp.frames_emitted} frames, "
            f"{esp.crc_corrupted} corrupted, {esp.gaps_injected} gaps, "
            f"{esp.resets_done} resets"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
