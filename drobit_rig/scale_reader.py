"""Serial reader for the ESP32 scale head.

Owns the serial port for the whole session: runs a START handshake until the
ESP is streaming, tags every valid frame with CLOCK_MONOTONIC, detects seq
gaps / ESP resets, and appends samples to <session_dir>/scale.parquet in one
row group per flush.

Opening the port on a CP210x/CH340 bridge can pulse DTR/RTS and reset the
ESP32 despite dsrdtr=False, so a single fire-and-forget START is easily lost
during the ~1-2 s boot. Instead of sending START once, the reader keeps
(re)sending it until a valid sample frame with our session id arrives, and
also re-sends immediately whenever the ESP signals it is idle (a status frame,
a seq reset, or frames tagged with the wrong session id). See _handle_frame /
_send_start for the single "ensure started" mechanism the retransmit,
reset-recovery, and stale-session paths all share.

Runs standalone:  python -m drobit_rig.scale_reader --session-dir ... --session-id ...
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import threading
import time
from pathlib import Path
from typing import IO

import pyarrow as pa
import pyarrow.parquet as pq
import serial

from . import protocol
from .logging_setup import setup_logging

_LOG = logging.getLogger("scale_reader")

SCHEMA = pa.schema(
    [
        ("session", pa.uint32()),
        ("seq", pa.uint32()),
        ("esp_us", pa.uint64()),
        ("rpi_mono_ns", pa.uint64()),
        ("raw", pa.int32()),
    ]
)


def mono_ns() -> int:
    return time.clock_gettime_ns(time.CLOCK_MONOTONIC)


class ParquetSink:
    """Buffers samples in RAM, writes one Parquet row group per flush()."""

    def __init__(self, path: Path) -> None:
        self._writer = pq.ParquetWriter(path, SCHEMA)
        self._session: list[int] = []
        self._seq: list[int] = []
        self._esp_us: list[int] = []
        self._rpi_mono_ns: list[int] = []
        self._raw: list[int] = []
        self.rows_written = 0

    def __len__(self) -> int:
        return len(self._seq)

    def append(self, session: int, seq: int, esp_us: int, rpi_mono_ns: int, raw: int) -> None:
        self._session.append(session)
        self._seq.append(seq)
        self._esp_us.append(esp_us)
        self._rpi_mono_ns.append(rpi_mono_ns)
        self._raw.append(raw)

    def flush(self) -> int:
        n = len(self._seq)
        if n == 0:
            return 0
        table = pa.table(
            {
                "session": self._session,
                "seq": self._seq,
                "esp_us": self._esp_us,
                "rpi_mono_ns": self._rpi_mono_ns,
                "raw": self._raw,
            },
            schema=SCHEMA,
        )
        self._writer.write_table(table)
        self._session.clear()
        self._seq.clear()
        self._esp_us.clear()
        self._rpi_mono_ns.clear()
        self._raw.clear()
        self.rows_written += n
        return n

    def close(self) -> None:
        self.flush()
        self._writer.close()


class ScaleReader:
    def __init__(
        self,
        *,
        port: str,
        baud: int,
        session_id: int,
        session_dir: Path,
        flush_interval_s: float = 60.0,
        status_interval_s: float = 5.0,
        read_timeout_s: float = 0.2,
        start_retransmit_interval_s: float = 2.0,
        start_warn_after_s: float = 30.0,
    ) -> None:
        self._port = port
        self._baud = baud
        self._session_id = session_id
        self._session_dir = session_dir
        self._flush_interval_s = flush_interval_s
        self._status_interval_s = status_interval_s
        self._read_timeout_s = read_timeout_s
        # How often to (re)send START while we are not yet streaming, and how
        # long to wait before warning that the handshake still hasn't landed.
        self._start_retransmit_interval_s = start_retransmit_interval_s
        self._start_warn_after_s = start_warn_after_s

        self._parser = protocol.FrameParser()
        self._expected_seq: int | None = None
        # Handshake state. _started flips True once a sample frame carrying our
        # session id arrives; until then (and again after a reset) the run loop
        # keeps retransmitting START. _last_start_send throttles every START
        # sender so the reset/stale-session paths can't spam the ESP.
        self._started = False
        self._last_start_send = float("-inf")
        self._handshake_start = 0.0
        self._handshake_warned = False

        self.samples = 0
        self.missing_samples = 0
        self.gap_events = 0
        self.resets = 0
        self.serial_reconnects = 0
        self.start_sends = 0
        self.temp_frames = 0

    # -- serial ----------------------------------------------------------

    def _open_serial(self) -> serial.Serial:
        # CP2102/CH340 boards wire DTR/RTS to EN/IO0, so toggling them resets
        # the ESP. Configure everything on an unopened Serial and pre-set
        # dtr/rts False so pyserial applies "deasserted" in one step at
        # open() instead of pulsing. Some adapters still glitch the lines on
        # open; _handle_frame() treats the resulting seq reset gracefully.
        ser = serial.Serial()
        ser.port = self._port
        ser.baudrate = self._baud
        ser.timeout = self._read_timeout_s
        ser.dsrdtr = False
        ser.rtscts = False
        ser.xonxoff = False
        ser.dtr = False
        ser.rts = False
        ser.open()
        return ser

    def _open_serial_with_retry(self, stop: threading.Event) -> serial.Serial | None:
        """Retry open until it succeeds or we are told to stop.

        The device may enumerate late (boot autostart) or drop out mid-run;
        keep trying with capped backoff rather than crashing the session. On
        success we begin a fresh handshake and fire the first START; the run
        loop keeps retransmitting until the ESP actually streams.
        """
        delay = 1.0
        while not stop.is_set():
            try:
                ser = self._open_serial()
            except (serial.SerialException, OSError) as exc:
                _LOG.warning("cannot open %s: %s (retrying in %.0fs)", self._port, exc, delay)
                stop.wait(delay)
                delay = min(delay * 2, 30.0)
                continue
            _LOG.info("opened %s @ %d baud", self._port, self._baud)
            self._begin_handshake()
            if not self._send_start(ser, "initial", force=True):
                # force=True bypasses the throttle, so a False here is a write
                # error: the port dropped between open and the first START.
                _LOG.warning("port dropped while sending initial START; reopening")
                try:
                    ser.close()
                except Exception:
                    pass
                stop.wait(delay)
                continue
            return ser
        return None

    # -- START handshake ---------------------------------------------------

    def _begin_handshake(self) -> None:
        """(Re)enter the "not yet streaming" state and restart the warn clock.

        Called on (re)connect and whenever the ESP tells us it reset, so the
        run loop resumes retransmitting START and the 30 s warning measures
        from this fresh attempt.
        """
        self._started = False
        self._handshake_start = time.monotonic()
        self._handshake_warned = False

    def _send_start(self, ser: serial.Serial, reason: str, *, force: bool = False) -> bool:
        """(Re)send ``START <session_id>``; return True iff it was written.

        Throttled to the retransmit interval so the reset/stale-session paths
        can fire freely without flooding the ESP. ``force`` skips the throttle
        for events that mean "start now" (initial open, a status frame).
        """
        now = time.monotonic()
        if not force and now - self._last_start_send < self._start_retransmit_interval_s:
            return False
        try:
            ser.write(protocol.build_start(self._session_id))
            ser.flush()
        except (serial.SerialException, OSError) as exc:
            _LOG.error("failed to send START (%s): %s", reason, exc)
            return False
        self._last_start_send = now
        self.start_sends += 1
        _LOG.info("sent START %d (%s)", self._session_id, reason)
        return True

    # -- frame handling ----------------------------------------------------

    def _record_gap(self, gaps_file: IO[str], record: dict) -> None:
        gaps_file.write(json.dumps(record) + "\n")
        gaps_file.flush()

    def _note_wrong_session(
        self, frame: protocol.Frame, now_ns: int, gaps_file: IO[str], ser: serial.Serial,
    ) -> None:
        """Handle a frame carrying someone else's session id.

        Means the ESP is not (yet) running our session: our START was lost
        during its boot, or a previous run's session is still streaming. Re-arm
        it; a foreign seq space is meaningless against ours, so this is never
        counted as a gap. We only log/record when a START actually goes out
        (throttled) so a burst of stale frames can't spam the gaps log.
        """
        self._started = False
        if self._send_start(ser, "wrong session %d" % frame.session):
            _LOG.warning(
                "frame with wrong session %d (want %d, seq=%d): stale session, "
                "re-sent START", frame.session, self._session_id, frame.seq,
            )
            self._record_gap(gaps_file, {
                "kind": "wrong_session",
                "rpi_mono_ns": now_ns,
                "got_session": frame.session,
                "got_seq": frame.seq,
            })

    def _handle_frame(
        self, frame: protocol.Frame, now_ns: int, sink: ParquetSink,
        gaps_file: IO[str], ser: serial.Serial,
    ) -> None:
        # A status frame means the ESP is idle: freshly booted (its DTR/RTS-
        # pulse reset ate our first START) or just watchdog-reset. Take it as
        # the cue to (re)send START now instead of waiting for the next
        # retransmit tick, and treat ourselves as un-started again.
        if frame.type is protocol.FrameType.STATUS:
            _LOG.info("STATUS frame (seq=%d): ESP idle, (re)sending START", frame.seq)
            self._begin_handshake()
            # The status frame is itself the reset signal; the next stream will
            # restart at seq 0 after our START. Drop the stale baseline so that
            # fresh seq 0 is not mis-recorded as a backwards "reset".
            self._expected_seq = None
            self._send_start(ser, "status frame", force=True)
            return

        wrong_session = frame.session != self._session_id

        # seq increments per frame regardless of type, so gap tracking covers
        # pong frames too even though only samples are stored.
        if self._expected_seq is not None and frame.seq != self._expected_seq:
            if frame.seq > self._expected_seq:
                if wrong_session:
                    # A forward jump in a foreign session's seq space is not a
                    # gap in our stream - it's a stale session. Re-arm without
                    # recording a phantom gap.
                    self._note_wrong_session(frame, now_ns, gaps_file, ser)
                else:
                    missing = frame.seq - self._expected_seq
                    self.missing_samples += missing
                    self.gap_events += 1
                    _LOG.warning(
                        "seq gap: expected %d got %d (%d missing)",
                        self._expected_seq, frame.seq, missing,
                    )
                    self._record_gap(gaps_file, {
                        "kind": "gap",
                        "rpi_mono_ns": now_ns,
                        "expected_seq": self._expected_seq,
                        "got_seq": frame.seq,
                        "missing": missing,
                    })
            else:
                # seq went backwards: ESP reset (brown-out, watchdog, DTR
                # glitch) or a duplicate START. Log, record, re-enter the
                # handshake, keep going.
                self.resets += 1
                self._begin_handshake()
                _LOG.warning(
                    "seq went backwards (expected %d, got %d) - ESP reset? "
                    "session=%d", self._expected_seq, frame.seq, frame.session,
                )
                self._record_gap(gaps_file, {
                    "kind": "reset",
                    "rpi_mono_ns": now_ns,
                    "expected_seq": self._expected_seq,
                    "got_seq": frame.seq,
                    "missing": None,
                })
                if wrong_session:
                    # The reset dropped our session id; re-arm it (throttled)
                    # so subsequent rows are tagged with the right session.
                    self._send_start(ser, "reset")
        elif wrong_session:
            # First frame after (re)connect, or a contiguous frame, but from
            # the wrong session: re-arm without counting a gap.
            self._note_wrong_session(frame, now_ns, gaps_file, ser)

        self._expected_seq = (frame.seq + 1) & protocol.U32_MAX

        if frame.type is protocol.FrameType.SAMPLE:
            if not wrong_session and not self._started:
                _LOG.info(
                    "handshake complete: streaming session %d (seq=%d)",
                    self._session_id, frame.seq,
                )
                self._started = True
            sink.append(frame.session, frame.seq, frame.esp_us, now_ns, frame.raw)
            self.samples += 1
        elif frame.type is protocol.FrameType.TEMP:
            self._record_temperature(frame, now_ns)
            self.temp_frames += 1
        else:
            _LOG.info("received %s frame (seq=%d)", frame.type.name, frame.seq)

    def _record_temperature(self, frame: protocol.Frame, now_ns: int) -> None:
        """Append one temperature reading to temperature.jsonl. TEMP frames are
        rare (sub-Hz), so open/append/close per frame keeps it simple and
        crash-safe. `raw` is the sensor's value in device-specific units."""
        line = json.dumps({
            "rpi_mono_ns": now_ns,
            "esp_us": frame.esp_us,
            "session": frame.session,
            "raw": frame.raw,
        })
        with open(self._session_dir / "temperature.jsonl", "a") as temp_file:
            temp_file.write(line + "\n")

    # -- status ------------------------------------------------------------

    def _write_status(self, sink: ParquetSink, connected: bool) -> None:
        status = {
            "session_id": self._session_id,
            "started": self._started,
            "start_sends": self.start_sends,
            "samples": self.samples,
            "missing_samples": self.missing_samples,
            "gap_events": self.gap_events,
            "resets": self.resets,
            "temp_frames": self.temp_frames,
            "crc_errors": self._parser.crc_errors,
            "bytes_discarded": self._parser.bytes_discarded,
            "unknown_types": self._parser.unknown_types,
            "rows_written": sink.rows_written,
            "buffered": len(sink),
            "serial_connected": connected,
            "serial_reconnects": self.serial_reconnects,
            "updated_mono_ns": mono_ns(),
        }
        tmp = self._session_dir / "status.json.tmp"
        tmp.write_text(json.dumps(status, indent=2))
        os.replace(tmp, self._session_dir / "status.json")

    # -- main loop -----------------------------------------------------------

    def run(self, stop: threading.Event) -> int:
        _LOG.info(
            "scale reader starting: port=%s baud=%d session=%d dir=%s",
            self._port, self._baud, self._session_id, self._session_dir,
        )
        sink = ParquetSink(self._session_dir / "scale.parquet")
        ser: serial.Serial | None = None
        try:
            with open(self._session_dir / "gaps.jsonl", "a") as gaps_file:
                last_flush = time.monotonic()
                last_status = time.monotonic()
                while not stop.is_set():
                    if ser is None:
                        ser = self._open_serial_with_retry(stop)
                        if ser is None:
                            break  # stop requested while waiting for the port
                    try:
                        data = ser.read(4096)
                    except (serial.SerialException, OSError) as exc:
                        _LOG.warning("serial error: %s - reconnecting", exc)
                        try:
                            ser.close()
                        except Exception:
                            pass
                        ser = None
                        self.serial_reconnects += 1
                        continue
                    if data:
                        for frame in self._parser.feed(data):
                            self._handle_frame(
                                frame, mono_ns(), sink, gaps_file, ser
                            )
                    now = time.monotonic()
                    # START handshake: keep retransmitting until we are
                    # streaming, then stop. A status frame or reset can drop us
                    # back into this state mid-run, which is the whole point.
                    if not self._started:
                        if now - self._last_start_send >= self._start_retransmit_interval_s:
                            self._send_start(ser, "retransmit")
                        if (
                            not self._handshake_warned
                            and now - self._handshake_start >= self._start_warn_after_s
                        ):
                            _LOG.warning(
                                "no valid stream %.0fs after opening %s "
                                "(session=%d); still retransmitting START",
                                self._start_warn_after_s, self._port, self._session_id,
                            )
                            self._handshake_warned = True
                    if now - last_flush >= self._flush_interval_s:
                        n = sink.flush()
                        last_flush = now
                        _LOG.info("flushed %d rows (%d total)", n, sink.rows_written)
                    if now - last_status >= self._status_interval_s:
                        self._write_status(sink, connected=True)
                        last_status = now
        finally:
            _LOG.info("shutting down: flushing %d buffered rows", len(sink))
            if ser is not None:
                try:
                    ser.write(protocol.build_stop())
                    ser.flush()
                    _LOG.info("sent STOP")
                except (serial.SerialException, OSError) as exc:
                    _LOG.warning("could not send STOP: %s", exc)
                try:
                    ser.close()
                except Exception:
                    pass
            sink.close()
            self._write_status(sink, connected=False)
            _LOG.info(
                "done: %d samples, %d missing in %d gaps, %d resets, %d crc errors",
                self.samples, self.missing_samples, self.gap_events,
                self.resets, self._parser.crc_errors,
            )
        return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", default="/dev/esp-scale")
    ap.add_argument("--baud", type=int, default=921_600)
    ap.add_argument("--session-id", type=int, required=True)
    ap.add_argument("--session-dir", type=Path, required=True)
    ap.add_argument("--flush-interval", type=float, default=60.0)
    ap.add_argument("--status-interval", type=float, default=5.0)
    ap.add_argument("--start-retransmit-interval", type=float, default=2.0)
    ap.add_argument("--start-warn-after", type=float, default=30.0)
    args = ap.parse_args(argv)

    setup_logging("scale_reader", args.session_dir)
    stop = threading.Event()

    def _on_signal(signum: int, _frame: object) -> None:
        _LOG.info("received %s", signal.Signals(signum).name)
        stop.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    reader = ScaleReader(
        port=args.port,
        baud=args.baud,
        session_id=args.session_id,
        session_dir=args.session_dir,
        flush_interval_s=args.flush_interval,
        status_interval_s=args.status_interval,
        start_retransmit_interval_s=args.start_retransmit_interval,
        start_warn_after_s=args.start_warn_after,
    )
    return reader.run(stop)


if __name__ == "__main__":
    raise SystemExit(main())
