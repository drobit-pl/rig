# drobit-rig

Raw data collection for the poultry scale development rig. Records the 80 Hz
load-cell stream from the ESP32 and synchronized video from the Pi camera —
**no on-device processing**; everything is analyzed offline.

## Hardware assumptions

- Raspberry Pi 5 (8 GB), Raspberry Pi OS Bookworm 64-bit
- NVMe SSD mounted at `/data` (ext4, `noatime`) — all data lands under
  `/data/sessions/`
- Pi Camera Module on the **cam0** port, `rpicam-vid` working
  (`rpicam-hello --list-cameras` to verify)
- ESP32 with an ADS1232 reading the load cell at 80 SPS, streaming binary
  frames over USB serial at **921600 baud** (protocol in
  `drobit_rig/protocol.py`). CP2102 / CH340 / native USB all fine — the udev
  rule gives it a stable name `/dev/esp-scale`.

Note: CP2102/CH340 boards reset the ESP when DTR toggles. The reader opens
the port with DTR/RTS deasserted and no flow control, and if the ESP resets
anyway (seq goes backwards) it logs it, records it in `gaps.jsonl`, re-arms
the session with START and keeps going.

## Install

```bash
sudo apt install python3-venv rpicam-apps   # rpicam-apps usually preinstalled
sudo mkdir -p /data/drobit-rig /data/sessions
sudo chown "$USER" /data/drobit-rig /data/sessions

git clone <repo-url> /data/drobit-rig/src
python3 -m venv /data/drobit-rig/venv
/data/drobit-rig/venv/bin/pip install -e /data/drobit-rig/src

# stable serial device name (edit the file: keep only your adapter's line!)
sudo cp /data/drobit-rig/src/deploy/99-esp-scale.rules /etc/udev/rules.d/
sudo udevadm control --reload && sudo udevadm trigger
ls -l /dev/esp-scale

# serial port access without sudo
sudo usermod -aG dialout "$USER"   # then log out/in
```

Optionally add the venv's bin to PATH, or symlink:
`sudo ln -s /data/drobit-rig/venv/bin/drobit-rig /usr/local/bin/`.

## Run

```bash
drobit-rig start --breed "Ross 308" --placement-date 2026-06-01 \
                 --house H1 --pen P2 --bird-count 20000 --note "cycle day 35"
drobit-rig status                       # samples, gaps, disk free, segment count
drobit-rig mark-weight --grams 2450 --bird-id B7   # ground-truth reference weight
drobit-rig calibrate --grams 0 --dwell 5           # empty plate (then a known mass)
drobit-rig stop                         # flushes, writes end timestamps, event scan
```

One session at a time (lock file `/data/sessions/current.json`). `start`
spawns two detached workers — the scale reader and the video recorder — so
you can log out; `stop` SIGTERMs them and waits for a clean flush.

`mark-weight` and `calibrate` are run *during* a session: they timestamp into
the running session (aligned to `scale.parquet` by `rpi_mono_ns`).
`mark-weight` logs an independent scale reading — the truth the detector's
estimates are bias-corrected against. `calibrate` records a hold of a known
mass (`--grams 0` = empty) so the offline `compute_calibration` can derive
raw→grams and track gain drift.

Session directory layout:

```
/data/sessions/20260704_143012_1783538212/
├── meta.json           session id, wall↔monotonic bridge, deployment, config
├── scale.parquet       session, seq, esp_us, rpi_mono_ns, raw   (80 Hz)
├── temperature.jsonl   periodic ESP32 on-die temperature (raw), for drift
├── reference_weights.jsonl  ground-truth weights from `mark-weight`
├── calibration.jsonl   known-mass hold intervals from `calibrate`
├── gaps.jsonl          seq gaps and ESP resets
├── status.json         live counters (updated every 5 s)
├── video/seg_00000.h264 ...   5-min segments
├── video_index.jsonl   {seg, file, start_mono_ns, fps, source} per segment
├── events.jsonl        |signal−baseline| > threshold intervals (navigation aid)
├── logs/*.log          rotating logs per component
└── done                present iff the session was stopped cleanly
```

`meta.json` carries a `deployment` block (breed, placement date, computed
`cycle_day`, house/pen, bird count) so the analysis side can index the breed
growth curve and stratify across the cycle, plus device metrology in `config`
(`load_cell_capacity_g`, `ads1232_gain`, `ads1232_rate_sps`).

Tune with flags: `--fps`, `--width/--height`, `--segment-ms`, `--port`,
`--baud`, `--flush-interval`, `--threshold` (event scan, raw ADC counts),
`--root` or `DROBIT_RIG_ROOT` (sessions root — handy for desk testing).

### Desk testing without hardware

```bash
python -m tests.fake_esp --gap-every 500 --reset-after 60
# prints a pty path, then in another shell:
drobit-rig --root /tmp/rig-test --port /dev/ttys011 start --note "desk test"
```

(Video will log an error and give up if `rpicam-vid` is missing; the scale
reader runs regardless. On macOS add `--baud 115200`: ptys there reject the
non-standard 921600 rate. Irrelevant on the Pi.)

## Timestamps: how to line things up

Everything is CLOCK_MONOTONIC (`rpi_mono_ns`); wall-clock time appears
exactly twice, in `meta.json`:

- `wall_clock` + `mono_ns` — captured back to back at `start`
- `end_wall_clock` + `end_mono_ns` — same at `stop`

To convert any `rpi_mono_ns` to calendar time:
`wall_clock + (rpi_mono_ns − mono_ns)`.

**Sanity check** (also catches NTP steps mid-session): the two bridges must
agree —

```python
(end_wall − wall) − (end_mono_ns − mono_ns)  →  should be ~0 (< a few ms)
```

If it isn't ~0, the wall clock stepped during the session; trust the
monotonic column and the *start* bridge.

Video accuracy caveat: `start_mono_ns` in `video_index.jsonl` is derived
from the moment the `rpicam-vid` process was spawned plus per-segment PTS
(`source: "pts"`) or nominal segment durations (`source: "estimate"`).
Camera pipeline startup latency (a few hundred ms) is not observable, so
absolute video↔scale alignment is good to a few hundred ms. For tighter
alignment, put a visible event in frame (LED blink, tap the scale) and match
it against `scale.parquet`.

Crash caveat: `scale.parquet`'s footer is written on clean shutdown. If the
Pi loses power mid-session the file needs footer recovery — the row groups
(one per 60 s flush) are still on disk; recover offline with
`pyarrow`-based tools before analysis. `done` missing ⇒ treat as dirty.

## Pulling data off the device

```bash
rsync -avP --exclude 'current.json' pi@rig:/data/sessions/ ./sessions/
# only finished sessions:
ssh pi@rig 'ls -d /data/sessions/*/done' | xargs -n1 dirname | \
  rsync -avP --files-from=- pi@rig:/ ./sessions/
```

Disk math: scale data is trivial (~2 MB/h compressed); video dominates at
roughly 1–2 GB/h at 1280×720@20.

## Unattended field deployment (optional)

`deploy/drobit-rig-autostart.service` starts a session on boot and stops it
cleanly on shutdown. It is a template and **disabled by default** — edit
`User=` and the venv path, then `systemctl enable` it explicitly. See the
comments in the file.

## Development

```bash
pip install -e '.[dev]'
pytest                    # includes a ~10 s integration test over a pty
```

No camera or ESP32 needed for tests: `tests/fake_esp.py` emulates the serial
protocol (with CRC error / gap / reset injection) and `tests/fake_rpicam.py`
stands in for `rpicam-vid`.
