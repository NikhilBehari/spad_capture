# Configuration options

A single YAML config drives every run. All fields are optional. Field
defaults are listed below; CLI flags override the file.

## `sensor`

| Field | Default | Meaning |
|-------|---------|---------|
| `zone_mode` | `3x3_wide` | Predefined SPAD map (see [Zone modes](#zone-modes)) or `custom` for a user-defined mask. |
| `range_mode` | `long` | `long` (~5 m, ~260 ps/bin) or `short` (~1 m, ~100 ps/bin). |
| `port` | `null` | Serial port, e.g. `/dev/ttyACM0`. `null` auto-detects. |
| `baudrate` | `2000000` | Must match the sketch. |
| `timeout_s` | `1.0` | Serial read timeout. |
| `init_wait_s` | `1.5` | Wait after open for the Arduino auto-reset. |
| `calibrate` | `false` | Run factory crosstalk calibration on the active mask at startup (adds ~3 s). See [Calibration](#calibration). CLI shortcut: `--calibrate`. |

### Zone modes

The 14 predefined maps from DS000693 §7.4.1. FoV values are taken from
datasheet Figures 30 and 31.

| `zone_mode` | `spad_map_id` | Output shape | FoV (H x V) | Notes |
|-------------|---------------|--------------|-------------|-------|
| `3x3_narrow` | 1 | (3, 3, 128) | 33° x 32° | Normal mode |
| `3x3_macro` | 2 | (3, 3, 128) | 33° x 47° | Macro mode |
| `3x3_macro_v2` | 3 | (3, 3, 128) | 33° x 47° | Macro variant |
| `4x4_narrow` | 4 | (4, 4, 128) | 33° x 47° | Time-multiplexed |
| `4x4_narrow_v2` | 5 | (4, 4, 128) | 33° x 47° | Time-multiplexed variant |
| `3x3_wide` | 6 | (3, 3, 128) | 41° x 52° | Default. Widest 3x3. |
| `4x4_wide` | 7 | (4, 4, 128) | 41° x 52° | Widest 4x4. Time-multiplexed. |
| `9_zone` | 8 | (3, 3, 128) | 33° x 47° | Irregular 9-zone layout |
| `9_zone_v2` | 9 | (3, 3, 128) | 33° x 47° | Variant |
| `3x6` | 10 | (3, 6, 128) | 33° x 60° | Time-multiplexed |
| `3x3_checker` | 11 | (3, 3, 128) | 33° x 32° | Checkerboard SPAD pattern |
| `3x3_checker_rev` | 12 | (3, 3, 128) | 33° x 32° | Reverse checkerboard |
| `4x4_narrow_v3` | 13 | (4, 4, 128) | 33° x 42° | Narrow time-multiplexed |
| `8x8` | 15 | (8, 8, 128) | 41° x 52° | TMF8828 mode. Four logical 4x4 measurements x 2 sub-captures each = 8 sub-captures per frame. Frame rate is roughly half that of a 3x3. |
| `custom` | 14 or 15 | (1, N, 128) | depends on selected SPADs | User-defined. See [`mask`](#mask-user-defined-spad-layout-zone_modecustom-only). `N = user zones + dummy pixels`. `map_id=14` when every zone fits visual rows 1..6; auto-promoted to `map_id=15` (half frame rate) when any zone uses rows 7..10. |

### SPAD geometry

- Angular subtense at optical center: **2.4° x 5.6°** per SPAD (datasheet
  §5).
- Physical pitch: 16.8 µm x 38.8 µm; focal length 400 µm.
- Active SPAD area: 18 columns x 10 rows.
- Lens FoV: 41° x 52° across the full active area. Narrower predefined
  maps occupy a centered sub-rectangle.

### Histogram interpretation

- 128 time bins per channel. Bin width: ~260 ps in long range, ~100 ps in
  short range.
- Per-frame `t = 0` is set by the reference SPAD in the VCSEL cavity. In
  raw histograms the reference pulse lands near bin 15.

## `firmware`

Runtime knobs forwarded to the Arduino over serial at startup. Every
field defaults to `null`; a `null` value sends no override command and
the firmware uses its baked-in default.

| Field | Default | Meaning |
|-------|---------|---------|
| `kilo_iterations` | `null` | VCSEL pulses per measurement, divided by 1024 (uint16). Higher = more signal, lower frame rate. Firmware-baked defaults: 10000 for 3x3 maps, 2500 for 4x4 maps, 10 for 8x8. Override explicitly when capturing 8x8 (for example, 2500). |
| `period_ms` | `null` | Inter-measurement period in milliseconds. Set below the ranging time for maximum throughput. |

## `mask` (user-defined SPAD layout, `zone_mode=custom` only)

Required when `sensor.zone_mode = custom`; ignored otherwise. Validated
offline by `spad mask validate <file>` and again at capture time, before
any output directory is created.

| Field | Default | Meaning |
|-------|---------|---------|
| `zones` | `null` | List of `{id, spads}`. Each zone groups SPADs that share one output histogram. `id` is a positive int (1, 2, ...); `spads` is a list of `[row, col]` coordinates. |
| `grid` | `null` | ASCII visualization of the 12-row x 18-col visual frame. Same digits become the same zone id. |

Provide either `zones` or `grid` (or both; they must agree).
Time-multiplexed mode (`map_id=15`) is selected automatically when any
zone touches visual rows 7..10.

### Coordinate system

- Scene point-of-view: `(row, col)` is what the sensor sees. Row 0 is the
  top of the scene; col 0 is the left edge.
- Addressable area: rows 1..10 x cols 0..17 in the AMS 12-row x 18-col
  visual frame (DS000693 Fig 30/31/32). Rows 0 and 11 are macro-extension
  placeholders, not selectable with the default `y_offset_2 = 0`.
- Rows 1..6 are writable in single-shot mode (`map_id = 14`, SPAD-1 page).
- Rows 7..10 are writable only in time-multiplexed mode (`map_id = 15`,
  SPAD-2 page). The library auto-promotes when any zone uses rows 7..10.
  Time-multiplexed capture halves the frame rate.
- In the grid form, rows 0 and 11 are filled with `x`; rows 1..10 contain
  user zones or `.`. The parser also accepts `_` and `-` as `.`.

### Grid symbols

| Symbol | Meaning |
|---|---|
| `1` to `9` | user zone id |
| `.` (or `_`, `-`) | free SPAD (selectable but not chosen) |
| `x` | unselectable SPAD |

Whitespace between cells is tolerated.

### Validation rules

Enforced by `spad mask validate`. Sources: DS000693 §7.4.1, AMS validator
source.

1. Each user zone has at least 2 adjacent SPADs (8-neighbor, including
   diagonal).
2. Coordinates lie inside the addressable area: rows 1..10, cols 0..17.
   Rows 0 and 11 (macro extension) are rejected.
3. At most 8 user zones per sub-capture (one per available TDC channel,
   2..9). Single-shot caps at 8 total zones; time-mux caps at 16
   (8 per sub-capture).
4. In time-multiplexed mode, each zone fits entirely within one
   sub-capture's row range (rows 1..6 or rows 7..10). Zones that straddle
   the boundary are rejected.
5. No duplicate SPAD coordinates within a zone or across zones.

### Example

```yaml
sensor:
  zone_mode: custom

mask:
  zones:
    - id: 1
      spads: [[6, 8], [6, 9]]      # central 2-SPAD horizontal pair
```

The `grid` form is more readable for multi-zone layouts; see
[configs/masks/four_corners.yaml](../configs/masks/four_corners.yaml).

### Output shape

`(1, N, 128)` where `N = user_zones + dummy_pixels`. User zones come
first in ascending `id` order, then any auto-added dummy pixels.

### Dummy pixels

The TMF8828 requires at least one zone in each TDC pair `(2|3, 4|5, 6|7,
8|9)` per sub-capture for electrical calibration to complete. When a mask
uses fewer than four zones, the missing pairs are filled with placeholder
zones at unused corners (channels 4, 6, 8 in that order). These are
persisted in the saved data and tagged with `is_dummy=true` so they can
be filtered on read.

### `metadata.json` bookkeeping

Every capture's `metadata.json` includes:

- `mask`: the YAML input.
- `resolved_zones`: the full ordered list of zones the device ran. Each
  entry is `{output_index, user_zone_id, channel, is_dummy, spads}`.
  Entries with `is_dummy=true` and `user_zone_id=0` are dummy pixels.
- `user_output_indices`: indices into the histogram array that correspond
  to user zones, in order.

### Filtering dummy pixels when loading

```python
from spad_capture.storage import load, user_zone_histograms

meta, frames = load("outputs/<run>/data.h5")
zone_ids, hists = user_zone_histograms(meta, frames)
# hists shape: (num_frames, num_user_zones, num_bins)
# zone_ids[i] is the user-supplied id for column i (e.g. [1, 2, 3, 4]).
# For predefined zone modes, zone_ids is None and hists is returned unchanged.
```

`spad_capture.mask.zone_index_map(mask)` returns `{user_zone_id:
output_index}` directly from a `CustomMask`.

## `capture`

| Field | Default | Meaning |
|-------|---------|---------|
| `mode` | `sequential` | `sequential`, `streaming`, `timed`, or `manual`. |
| `num_frames` | `10` | Frames to capture (sequential mode); frames per burst (manual mode). |
| `duration_s` | `null` | Wall-clock duration in seconds (streaming or timed). |
| `interval_s` | `0.0` | Sleep between frames in seconds. |
| `samples_per_frame` | `1` | Sensor frames averaged per output frame. |

### Modes

- **sequential**: capture exactly `num_frames`, then stop.
- **streaming**: capture continuously until Ctrl-C, or until `duration_s`
  elapses if set.
- **timed**: capture for exactly `duration_s` seconds.
- **manual**: capture one `num_frames` burst on startup, then pause the
  device until the next trigger. Trigger by pressing Enter in the
  terminal, or by clicking the Capture button in the live dashboard.

## `storage`

Each run creates `<root>/<run_dir>/` containing:

- `data.<ext>`: the frame stream
- `config.yaml`: resolved config (re-runnable as-is)
- `metadata.json`: capture metadata (timing, version, calibration, layout)

| Field | Default | Meaning |
|-------|---------|---------|
| `format` | `pkl` | `pkl`, `npy`, `h5`, or `none`. |
| `root` | `outputs` | Top-level folder. |
| `run_dir_template` | `{timestamp}_{zone_mode}_{range_mode}_{name}` | Subfolder name. Keys: `timestamp` (date), `zone_mode`, `range_mode`, `capture_mode`, `name`. Same-day collisions auto-suffix `_2`, `_3`, etc. |
| `data_filename` | `data` | Data file's basename inside the run-dir. |
| `save_metadata` | `true` | Write the `metadata.json` sidecar. |
| `save_resolved_config` | `true` | Write the `config.yaml` sidecar. |

The top-level `name` field (not part of `storage:`) sets the `{name}`
placeholder. Leave it `null` and the placeholder collapses to nothing.

### Format trade-offs

| Format | Streaming | Random access | Compression | Notes |
|--------|-----------|---------------|-------------|-------|
| `pkl` | append-only | sequential | none | Default. Works with anything. |
| `npy` | in-memory buffer | indexable | none | Flushes a single stack at close; companion `data_ts.npy` carries timestamps. |
| `h5` | streamed extendable dataset | indexable | gzip | Best for long captures. |
| `none` | n/a | n/a | n/a | Use with `--viz` for live-only runs. |

Load any format back with:

```python
from spad_capture.storage import load
metadata, frames = load("outputs/<run>/data.pkl")
```

## `viz` (live web dashboard)

| Field | Default | Meaning |
|-------|---------|---------|
| `enabled` | `false` | Start the dashboard server alongside capture. |
| `host` | `127.0.0.1` | Bind host. Use `0.0.0.0` or `--bind-all` to expose to the network. |
| `port` | `8888` | Bind port. |
| `update_hz` | `10.0` | Maximum dashboard refresh rate (frontend-side throttle). |

The dashboard renders the per-zone histogram grid and, when present, the
colocated RGB image. The same page handles live capture and saved-file
replay; replay adds prev / next / play-pause / scrub / rate controls.
The frontend (`spad_capture/viz/static/index.html`) consumes a binary
`/ws` stream documented in `viz/server.py`. Replace `index.html` to
provide a different dashboard.

## `rgb` (colocated Realsense)

| Field | Default | Meaning |
|-------|---------|---------|
| `enabled` | `false` | Open a Realsense camera alongside the SPAD. |
| `width` | `848` | Color stream width. |
| `height` | `480` | Color stream height. |
| `fps` | `30` | Color frame rate. |
| `serial_number` | `null` | Specific Realsense serial; `null` selects the first device. |
| `save_depth` | `false` | Save depth frames aligned to color. |
| `jpeg_quality` | `80` | JPEG quality (1 to 100) for the live preview. |

If `rgb.enabled = true` and the camera fails to open, the run aborts
before any output file is created. With `save_depth = true`, depth
frames are captured and aligned to color through the Realsense alignment
API. Aligned depth is stored alongside each SPAD frame in the chosen
storage format (`pkl` or `h5`).

## Calibration

The TMF8828 has no per-device factory calibration baked into silicon
(AN001015 §4.2). Every active SPAD mask, predefined or custom, requires
its own crosstalk calibration, and the host loads it on every power-up.
Without calibration the device falls back to a generic internal default
and reports `CALIBRATION_STATUS = 0x31`; ranging still works, but
per-channel VCSEL and optical crosstalk are not subtracted.

To run calibration for a capture:

```bash
spad capture --calibrate ...
```

Calibration takes a few seconds and must run in a dark housing with no
target inside 40 cm (DS000693 §7.3). The calibration lives in device RAM
for the session only.

## CLI

Every config field has a matching CLI flag. See `spad capture --help`.
Common patterns:

```bash
# default config
spad capture

# zone override, streaming, with live dashboard
spad capture --zone 8x8 --mode streaming --viz

# higher iteration count for SNR
spad capture --zone 4x4_wide --kilo-iter 20000 -n 5

# named run
spad capture --name experiment_3 --zone 3x3_macro

# custom mask
spad capture --mask configs/masks/four_center_quads.yaml -n 5
spad mask validate configs/masks/four_center_quads.yaml
spad mask preview  configs/masks/four_center_quads.yaml
```
