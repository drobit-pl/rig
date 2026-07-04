"""Logging: stderr + rotating file under <session_dir>/logs/."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def setup_logging(component: str, session_dir: Path | None = None,
                  level: int = logging.INFO) -> logging.Logger:
    """Configure the root logger for one rig process.

    Meant to be called exactly once from a process entry point (main()).
    Library/test code should use plain logging.getLogger() and let the host
    configure handlers.
    """
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    fmt = logging.Formatter(_FORMAT)
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(fmt)
    root.addHandler(stderr_handler)

    if session_dir is not None:
        log_dir = session_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_dir / f"{component}.log", maxBytes=5 * 1024 * 1024, backupCount=3
        )
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)

    return logging.getLogger(component)
