# Docs

Starting point for spad_capture. `docs.md` covers the conventions shared by
both sensors: capture modes, storage, the dashboard and Realsense capture.
Sensor-specific configuration, zone selection and firmware:

- [tmf.md](tmf.md) — TMF8828: SPAD masks, calibration, firmware.
- [st.md](st.md) — VL53L8CH: histogram window, ROI, firmware.

A single YAML config drives every run. All fields are optional; CLI flags
override the file. Each config declares `backend: tmf | st`, checked against the
command it is run with.

## `backend`

| Field | Default | Meaning |
|-------|---------|---------|
| `backend` | none | `tmf` or `st`. Optional, but every shipped config sets it: running a config under the other backend then fails by name instead of as a field-type error. |

## `capture`

| Field | Default | Meaning |
|-------|---------|---------|
| `mode` | `sequential` | `sequential`, `timed`, or `manual`. |
| `num_frames` | `10` | Frames to capture (sequential mode); frames per burst (manual mode). |
| `duration_s` | `60` | Wall-clock duration in seconds (timed mode). |
| `interval_s` | `0.0` | Sleep between frames in seconds. |
| `samples_per_frame` | `1` | Sensor frames averaged per output frame. |

### Modes

- **sequential**: capture exactly `num_frames`, then stop.
- **timed**: capture continuously for exactly `duration_s` seconds (60 by
  default), then stop.
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
| `align_depth` | `true` | Align depth to color; needs the color stream. |
| `ir_left` | `false` | Save the left IR image (infrared 1). |
| `ir_right` | `false` | Save the right IR image (infrared 2). |
| `ir_no_dots` | `false` | Hold the dot projector off so IR carries no projected pattern. |
| `jpeg_quality` | `80` | JPEG quality (1 to 100) for the live preview. |

If `rgb.enabled = true` and the camera fails to open, the run aborts
before any output file is created. With `save_depth = true`, depth
frames are captured and aligned to color through the Realsense alignment
API. Aligned depth is stored alongside each SPAD frame in the chosen
storage format (`pkl` or `h5`).

### Dot projector

The Realsense projects an IR dot pattern. Depth needs it to come out dense, and
it stamps the same pattern across the IR images. `ir_no_dots` holds the projector
off, leaving IR lit only by the scene. With `save_depth` set the projector is
pulsed: on for depth, off for IR.

Measured on a D435i, projector on then off: IR Laplacian variance 932 then 37,
depth 98% valid then 41%. Dot-free IR costs depth density, so the two are
separate switches.

### Realsense on macOS

macOS routes cameras through system daemons that claim the device as soon as
anything enumerates it. They respawn on demand, so only a process that can
release them opens the camera: **Realsense capture on macOS needs root**.

Nothing prompts for a password. A run that needs root stops and prints the
command to repeat under `sudo`:

```
Could not open the Realsense as this user (last error: No device connected).
  Something else holds the camera. On macOS that is usually the system
  camera daemons, and only root can release them.
  Run: sudo /path/to/spad tmf capture --rgb
```

Each open attempt must deliver frames before it counts. A Realsense can start a
pipeline and then send nothing, which would otherwise write a run of empty
frames.

Attempts are few and the waits between them long. A camera another process just
released needs seconds before it delivers frames, and each attempt disturbs it.
Expect the first capture after a long-running process releases the camera to need
a second run.

When no camera enumerates, it has dropped off the USB bus. Unplug it, wait two
seconds, plug it back in.

Never `kill -9` a process holding the camera. SIGKILL mid-stream leaves it in a
power state the next open reports as `failed to set power state`, after which it
disappears from the bus. Both backends catch `SIGINT` and `SIGTERM`, stop the
pipeline and flush the run.

Linux needs none of this.

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
| `rgb.ir_no_dots` | `--ir-no-dots` / `--ir-dots` |
