"""Rig defaults. CLI flags override these; DROBIT_RIG_ROOT overrides data_root."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _default_root() -> Path:
    return Path(os.environ.get("DROBIT_RIG_ROOT", "/data/sessions"))


@dataclass(frozen=True, slots=True)
class RigConfig:
    data_root: Path = field(default_factory=_default_root)
    port: str = "/dev/esp-scale"
    baud: int = 921_600
    width: int = 1280
    height: int = 720
    fps: int = 20
    segment_ms: int = 300_000
    flush_interval_s: float = 60.0
    # |raw - baseline| above this (raw ADC counts) is flagged by events.py.
    event_threshold: int = 2000
    # -- device metrology (recorded in meta.json for reproducibility) ----------
    load_cell_capacity_g: int | None = None
    """Full-scale rating of the load cell in grams; sets the raw-count budget."""
    ads1232_gain: int = 128
    ads1232_rate_sps: int = 80
