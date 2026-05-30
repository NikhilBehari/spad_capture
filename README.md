# spad_capture

Capture pipeline for the AMS OSRAM TMF8828 SPAD sensor. Records per-zone
time-of-flight histograms from predefined or user-defined SPAD masks,
with an integrated live web dashboard.

Every CLI flag can be supplied as a YAML config and passed with `-c`. See
[docs/options.md](docs/options.md) for the full schema.

## Install

```bash
# set up environment
conda create -n spad_capture python=3.11 -y
conda activate spad_capture
pip install -e .

# flash the dev board 
# auto-installs required arduino-cli if missing
spad flash
```

## Capture

```bash
# default config - 3x3_wide, long_range, 10 frames
spad capture

# customize capture params
spad capture --zone 4x4_wide --range short --kilo-iter 5000 -n 20

# yaml-defined params
spad capture -c configs/8x8.yaml

# custom pixel zone mask
spad capture --mask configs/masks/four_center_quads.yaml
```

Add `--viz` for the live dashboard at `http://127.0.0.1:8888`.

## Key parameters

The main fields tuned per capture. Every field has a matching CLI flag
(`spad capture --help`); the full schema is in
[docs/options.md](docs/options.md).

```yaml
sensor:
  zone_mode: 3x3_wide | 8x8 | ...        # capture zone mode
  range_mode: long | short               # 5m vs 1.5m mode 

firmware:
  kilo_iterations:  e.g. 5000, 20000     # per-frame pulses 
  period_ms:        e.g. 0, 16, 100      # min frame interval

capture:
  mode:  sequential | streaming | timed | manual  # capture type; see docs

storage:
  format:  pkl | npy | h5 | none         # output file format

viz:
  enabled:  true | false                 # live capture dashboard
  host:     127.0.0.1 | 0.0.0.0          # bind host
  port:     e.g. 8888                    # bind port
```

## Custom masks

Author SPAD layouts in YAML using the AMS 12 x 18 visual frame from
DS000693 Fig 30/31/32. Each digit names a zone; zones with the same
digit share one output histogram.

```yaml
# Custom mask, four 2x2 zones around the optical center
mask:
  grid: |
    x x x x x x x x x x x x x x x x x x
    . . . . . . . . . . . . . . . . . .
    . . . . . . . . . . . . . . . . . .
    . . . . . . 1 1 . . 2 2 . . . . . .
    . . . . . . 1 1 . . 2 2 . . . . . .
    . . . . . . . . . . . . . . . . . .
    . . . . . . . . . . . . . . . . . .
    . . . . . . 3 3 . . 4 4 . . . . . .
    . . . . . . 3 3 . . 4 4 . . . . . .
    . . . . . . . . . . . . . . . . . .
    . . . . . . . . . . . . . . . . . .
    x x x x x x x x x x x x x x x x x x
```

```bash
spad mask validate configs/masks/four_center_quads.yaml
spad mask preview  configs/masks/four_center_quads.yaml
spad capture --mask configs/masks/four_center_quads.yaml
```

Coordinate convention, validation rules, dummy-pixel behavior, and the
single-shot vs. time-multiplexed split are in
[docs/options.md § mask](docs/options.md#mask-user-defined-spad-layout-zone_modecustom-only).

## Saved captures

Output writes to `outputs/<run>/data.pkl` by default. Switch with `-f
npy`, `-f h5`, or `-f none`. Trade-offs:
[docs/options.md § storage](docs/options.md#storage).

Read in Python:

```python
from spad_capture.storage import load, user_zone_histograms

meta, frames = load("outputs/<run>/data.h5")
zone_ids, hists = user_zone_histograms(meta, frames)
# hists: (n_frames, n_user_zones, 128); zone_ids: e.g. [1, 2, 3, 4]
```

Replay through the dashboard:

```bash
spad viz --source outputs/<run>/data.h5
```

## Documentation

- [docs/options.md](docs/options.md): every config field across `sensor`, `firmware`, `capture`, `storage`, `viz`, `rgb`, and `mask`.
- [docs/firmware.md](docs/firmware.md): Arduino sketch architecture, serial protocol, when a re-flash is required.

## Layout

```
configs/                       example YAML configs and masks
spad_capture/
├── config.py                  pydantic schema and YAML loader
├── sensor.py                  TMF8828 driver
├── mask.py                    custom-mask validator and byte serializer
├── capture.py                 sequential / streaming / timed / manual loops
├── storage.py                 pkl / npy / h5 writers
├── cli.py                     click entry points
├── firmware/tmf8828/          bundled Arduino sketch
└── viz/                       FastAPI + WebSocket server + static dashboard
```
