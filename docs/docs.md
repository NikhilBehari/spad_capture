# Docs

Starting point for spad_capture. `docs.md` covers the conventions shared by
both sensors: capture modes, storage, the dashboard and Realsense capture.
Sensor-specific configuration, zone selection and firmware:

- [tmf.md](tmf.md) — TMF8828: SPAD masks, calibration, firmware.
- [st.md](st.md) — VL53L8CH: histogram window, ROI, firmware.

A single YAML config drives every run. All fields are optional; CLI flags
override the file. Each config declares `sensor: tmf | st`, checked against the
command it is run with.

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
| `run_dir_template` | tmf: `{timestamp}_{sensor}_{zone_mode}_{range_mode}_{name}`<br>st: `{timestamp}_{sensor}_{mode}_{name}` | Per-run subfolder name. Keys: `timestamp` (date), `sensor`, `capture_mode`, `name`, plus the sensor's own (`zone_mode`, `range_mode` for tmf; `mode` for st). Empty placeholders collapse; same-day collisions auto-suffix `_2`, `_3`. |
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

## CLI

Flags shared by both backends. Per-backend flags are in
[tmf.md § CLI](tmf.md#cli) and [st.md § CLI](st.md#cli).

| Config field | Flag |
|---|---|
| `name` | `--name` |
| `capture.num_frames` | `-n`, `--num-frames` |
| `capture.duration_s` | `-d`, `--duration` |
| `capture.interval_s` | `-i`, `--interval` |
| `storage.format` | `-f`, `--format` |
| `storage.root` | `-o`, `--output-dir` |
| `viz.enabled` | `--viz` / `--no-viz` |
| `viz.port` | `--viz-port` |
| `viz.host` | `--bind-all` (sets `0.0.0.0`) |
| `rgb.enabled` | `--rgb` / `--no-rgb` |
| `rgb.save_depth` | `--save-depth` / `--no-save-depth` |
| `rgb.ir_left` | `--ir-left` / `--no-ir-left` |
| `rgb.ir_right` | `--ir-right` / `--no-ir-right` |
