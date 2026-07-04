"""Session lifecycle: start/stop/status, single-session lock.

A session is two detached child processes (scale reader + video recorder)
plus a directory under the data root:

    {root}/{YYYYMMDD_HHMMSS}_{session_id}/
        meta.json           written at start, end timestamps added at stop
        scale.parquet       80 Hz raw samples
        gaps.jsonl          seq gaps / ESP resets
        status.json         live counters from the scale reader
        video/seg_*.mp4|h264, video_index.jsonl
        events.jsonl        written by the post-session event scan
        logs/*.log
        done                marker: session was stopped cleanly

The single-session lock is {root}/current.json (created with O_EXCL). All
writes stay under the data root.

Note on START: the spec-level flow is "start sends START to the ESP", but
exactly one process may own the serial port, so the scale reader sends START
itself right after it opens the port (and STOP on shutdown).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from . import __version__, events
from .config import RigConfig

_LOG = logging.getLogger("session")

LOCK_NAME = "current.json"
STOP_TIMEOUT_S = 25.0  # > scale reader's worst-case flush + close


class SessionError(Exception):
    pass


def mono_ns() -> int:
    return time.clock_gettime_ns(time.CLOCK_MONOTONIC)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _git_commit() -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(Path(__file__).resolve().parent), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def read_lock(root: Path) -> dict[str, Any] | None:
    try:
        return json.loads((root / LOCK_NAME).read_text())
    except FileNotFoundError:
        return None
    except json.JSONDecodeError:
        _LOG.warning("corrupt lock file %s", root / LOCK_NAME)
        return None


def _spawn(module: str, args: list[str], stderr_path: Path) -> subprocess.Popen:
    # Detached (start_new_session): survives the CLI exiting, and gets reaped
    # by init when it dies. stderr goes to a file so crashes that happen
    # before logging is set up (import errors etc.) are not lost.
    stderr_file = open(stderr_path, "ab")
    try:
        return subprocess.Popen(
            [sys.executable, "-m", module, *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=stderr_file,
            start_new_session=True,
        )
    finally:
        stderr_file.close()  # the child holds its own copy of the fd


def start_session(cfg: RigConfig, note: str = "") -> Path:
    root = cfg.data_root
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / LOCK_NAME

    lock = read_lock(root)
    if lock is not None:
        if any(_pid_alive(p) for p in lock.get("pids", {}).values()):
            raise SessionError(
                f"a session is already running in {lock.get('session_dir')} "
                f"(pids {lock.get('pids')}); run 'drobit-rig stop' first"
            )
        _LOG.warning("removing stale lock (all pids dead): %s", lock_path)
        lock_path.unlink(missing_ok=True)

    # Claim the lock before doing anything else (O_EXCL closes the race
    # between two concurrent starts); fill in pids after spawning.
    fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    os.close(fd)

    try:
        # This wall/mono pair, captured back to back, is the only bridge
        # between monotonic timestamps and calendar time for this boot.
        wall = datetime.now().astimezone()
        mono = mono_ns()
        session_id = int(wall.timestamp()) & 0xFFFF_FFFF

        session_dir = root / f"{wall.strftime('%Y%m%d_%H%M%S')}_{session_id}"
        session_dir.mkdir()
        (session_dir / "video").mkdir()
        (session_dir / "logs").mkdir()

        meta = {
            "session_id": session_id,
            "wall_clock": wall.isoformat(),
            "mono_ns": mono,
            "note": note,
            "config": {
                "port": cfg.port,
                "baud": cfg.baud,
                "width": cfg.width,
                "height": cfg.height,
                "fps": cfg.fps,
                "segment_ms": cfg.segment_ms,
                "flush_interval_s": cfg.flush_interval_s,
                "git_commit": _git_commit(),
                "version": __version__,
            },
        }
        (session_dir / "meta.json").write_text(json.dumps(meta, indent=2))

        scale_proc = _spawn(
            "drobit_rig.scale_reader",
            [
                "--port", cfg.port,
                "--baud", str(cfg.baud),
                "--session-id", str(session_id),
                "--session-dir", str(session_dir),
                "--flush-interval", str(cfg.flush_interval_s),
            ],
            session_dir / "logs" / "scale_reader.stderr.log",
        )
        video_proc = _spawn(
            "drobit_rig.video",
            [
                "--session-dir", str(session_dir),
                "--width", str(cfg.width),
                "--height", str(cfg.height),
                "--fps", str(cfg.fps),
                "--segment-ms", str(cfg.segment_ms),
            ],
            session_dir / "logs" / "video.stderr.log",
        )

        lock_path.write_text(json.dumps({
            "session_dir": str(session_dir),
            "session_id": session_id,
            "pids": {"scale_reader": scale_proc.pid, "video": video_proc.pid},
            "started_mono_ns": mono,
        }, indent=2))
    except BaseException:
        lock_path.unlink(missing_ok=True)
        raise

    _LOG.info(
        "session %d started in %s (scale pid %d, video pid %d)",
        session_id, session_dir, scale_proc.pid, video_proc.pid,
    )
    return session_dir


def stop_session(cfg: RigConfig) -> Path:
    root = cfg.data_root
    lock = read_lock(root)
    if lock is None:
        raise SessionError("no session is running (no lock file)")
    session_dir = Path(lock["session_dir"])
    pids: dict[str, int] = lock.get("pids", {})

    for name, pid in pids.items():
        try:
            os.kill(pid, signal.SIGTERM)
            _LOG.info("sent SIGTERM to %s (pid %d)", name, pid)
        except ProcessLookupError:
            _LOG.warning("%s (pid %d) was not running", name, pid)

    deadline = time.monotonic() + STOP_TIMEOUT_S
    remaining = dict(pids)
    while remaining and time.monotonic() < deadline:
        remaining = {n: p for n, p in remaining.items() if _pid_alive(p)}
        time.sleep(0.2)
    clean = not remaining
    for name, pid in remaining.items():
        _LOG.error("%s (pid %d) did not exit in %.0fs; SIGKILL", name, pid, STOP_TIMEOUT_S)
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    # Second wall/mono bridge point; comparing the two pairs also gives a
    # cheap clock-drift sanity check (see README).
    wall_end = datetime.now().astimezone()
    mono_end = mono_ns()
    meta_path = session_dir / "meta.json"
    try:
        meta = json.loads(meta_path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        _LOG.error("meta.json missing/corrupt in %s", session_dir)
        meta = {}
    meta["end_wall_clock"] = wall_end.isoformat()
    meta["end_mono_ns"] = mono_end
    meta["clean_shutdown"] = clean
    meta_path.write_text(json.dumps(meta, indent=2))

    try:
        events.scan_session(session_dir, threshold=cfg.event_threshold)
    except Exception as exc:
        _LOG.warning("event scan failed (data is unaffected): %s", exc)

    (session_dir / "done").touch()
    (root / LOCK_NAME).unlink(missing_ok=True)
    _LOG.info("session stopped: %s (clean=%s)", session_dir, clean)
    return session_dir


def session_status(cfg: RigConfig) -> dict[str, Any]:
    root = cfg.data_root
    lock = read_lock(root)
    status: dict[str, Any] = {"running": False}

    try:
        usage = shutil.disk_usage(root)
        status["disk_free_gb"] = round(usage.free / 1e9, 2)
        status["disk_total_gb"] = round(usage.total / 1e9, 2)
    except OSError:
        pass

    if lock is None:
        done = sorted(root.glob("*/done")) if root.exists() else []
        if done:
            status["last_session_dir"] = str(done[-1].parent)
        return status

    session_dir = Path(lock["session_dir"])
    pids: dict[str, int] = lock.get("pids", {})
    alive = {name: _pid_alive(pid) for name, pid in pids.items()}
    status.update({
        "running": any(alive.values()),
        "session_dir": str(session_dir),
        "session_id": lock.get("session_id"),
        "processes": {
            name: {"pid": pids[name], "alive": alive[name]} for name in pids
        },
        "segments": len(list((session_dir / "video").glob("*seg_*.*")))
        if (session_dir / "video").exists() else 0,
    })
    try:
        reader = json.loads((session_dir / "status.json").read_text())
        status["scale"] = {
            "samples": reader.get("samples"),
            "missing_samples": reader.get("missing_samples"),
            "gap_events": reader.get("gap_events"),
            "resets": reader.get("resets"),
            "crc_errors": reader.get("crc_errors"),
            "serial_connected": reader.get("serial_connected"),
            "status_age_s": round(
                (mono_ns() - reader["updated_mono_ns"]) / 1e9, 1
            ) if "updated_mono_ns" in reader else None,
        }
    except (FileNotFoundError, json.JSONDecodeError):
        status["scale"] = None
    return status
