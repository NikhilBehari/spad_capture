# VL53L8CH

ST VL53L8CH. Per-zone CNH histograms in physical units over a configurable mm
window, on a 4x4 or 8x8 grid. Zones are selected as a rectangular ROI, applied
on the device before streaming. Shared fields: [docs.md](docs.md).

## The sensor

A multizone ToF SPAD on the **X-NUCLEO-53L8A1** shield, driven by a
**NUCLEO-F401RE** running the bundled stm32duino sketch. It returns one
normalised time histogram per zone over a configurable distance window.

| | |
|---|---|
| Zone grids | 4x4 (16 zones) or 8x8 (64 zones); no others exist |
| Native bin | **37.5348 mm** one-way per histogram bin |
| CNH | exponent-mantissa encoded on the device; the sketch decodes it to one float32 magnitude per bin before streaming |
| Connection | USB CDC over ST-LINK/V2.1 (VID `0x0483`), typically `/dev/ttyACM*`, 921600 baud |

## Commands

```bash
spad st detect                 # handshake: device id and firmware version
spad st info                   # resolved geometry: bins, window, CNH budget
spad st flash                  # build and upload the sketch
spad st capture                # capture frames
spad st viz                    # dashboard; --source replays a saved run
```

`detect` and `info` both run without capturing: `detect` confirms the link and
the board, `info` shows what a config resolves to before you commit to it.

## `sensor`

| Field | Default | Meaning |
|-------|---------|---------|
| `mode` | `4x4` | Zone grid: `4x4` (16 zones) or `8x8` (64). No other grids exist. |
| `start_mm` | `0.0` | Histogram window start, one-way mm. Snapped to a native-bin multiple. |
| `end_mm` | `2000.0` | Window end, one-way mm. |
| `bin_mm` | `37.5348` | *Requested* bin width; rounded to the nearest multiple of the native bin. |
| `ranging_frequency_hz` | `15` | Ranging rate. CNH constraint: 1..30 Hz. |
| `integration_time_ms` | `20` | Honored in `autonomous` only; continuous integrates to the ranging period. |
| `ranging_mode` | `continuous` | `continuous` or `autonomous`. |
| `zone` | `null` | Single output zone `[row, col]`. Takes precedence over `roi`. |
| `roi` | `null` | `{start_row, start_col, rows, cols}` rectangle. |
| `port` | `null` | Serial port. `null` auto-detects the ST-LINK VCP (VID `0483`). |
| `baudrate` | `921600` | Must match the sketch. |
| `timeout_s` | `2.0` | Serial read timeout. |
| `init_wait_s` | `2.0` | Wait after open for the MCU auto-reset. |

### Window and bins

Every window field is in physical units. Bin indices are derived on the host,
and only those integers reach the firmware.

`bin_mm` is a request: it is snapped to a whole multiple of the native bin.

```
binning_factor   = round(bin_mm / 37.5348)                              # >= 1
effective_bin_mm = binning_factor * 37.5348                             # what you get
start_bin        = round(start_mm / 37.5348)
num_bins         = round((end_mm - start_bin * 37.5348) / effective_bin_mm)
```

So the default `0..2000 mm` window resolves to 53 bins of 37.53 mm, ending at
1989.3 mm. `spad st info` prints the resolved plan, and `metadata.json` records
the requested-vs-delivered deltas under `sensor_layout.adjustments`.

### CNH budget

Bins cost on-device memory, capped at **6160 bytes**:

```
budget_bytes = zones * num_bins * 5 + zones * 5 + 28
```

This caps `num_bins` per grid. Exceeding it raises a config error naming the
largest `num_bins` that fits, rather than truncating silently.

| selection | zones | max `num_bins` | window at that max, native bins |
|---|---|---|---|
| 4x4 full grid | 16 | **75** | 0..2815 mm |
| 8x8 full grid | 64 | **18** | 0..676 mm (0..1351 mm at `bf=2`) |
| single `zone` | 1 | **255** | 0..9571 mm |

The 8x8 grid trades depth resolution for zone count: keep the bins coarse or
the window short. Selecting one zone or a small `roi` frees the budget and
reaches the 255-bin ceiling.

Zones are selected as a rectangle rather than per pixel: `zone` picks one,
`roi` picks a block, and the default is the full grid. The device applies the
selection before streaming, so frames arrive already shaped to it. The `mask`
and `firmware` sections are TMF-only and do not apply here.

## Firmware

### Architecture

One stm32duino sketch drives the sensor directly through ST's VL53LMZ ULD and
its CNH plugin, both vendored beside it in
`spad_capture/sensors/st/firmware/vl53l8ch_cnh/`. There is no second firmware
image: the sketch configures CNH, polls the sensor, decodes the
exponent-mantissa histogram to float32 on the MCU, and streams a framed binary
packet per measurement over the ST-LINK VCP at 921600 baud.

| | |
|---|---|
| MCU board | NUCLEO-F401RE (`STMicroelectronics:stm32:Nucleo_64:pnum=NUCLEO_F401RE`) |
| Sensor shield | X-NUCLEO-53L8A1 |
| Host link | USB CDC over ST-LINK/V2.1, VID:PID `0483:374b` |

### Live-tunable parameters

The host sends one `C` line before ranging; every field is an integer, so
millimetres never cross the wire. `spad st info` prints what a config resolves
to first.

| Parameter | YAML field | Wire field |
|---|---|---|
| Zone grid | `sensor.mode` | mode code (1 = 4x4, 2 = 8x8) |
| Window start | `sensor.start_mm` | `start_bin` |
| Bin count | `sensor.end_mm`, `bin_mm` | `num_bins`, `binning_factor` |
| Ranging rate | `sensor.ranging_frequency_hz` | frequency |
| Integration | `sensor.integration_time_ms` | integration |
| ROI | `sensor.zone`, `sensor.roi` | `agg_start_x/y`, `agg_cols/rows` |

The device emits frames already shaped to the ROI, so nothing is cropped or
reindexed on the host.

### When the sketch must be rebuilt

Anything compiled in: the I2C clock, the shield enable-pin mapping, the frame
format, or a new ULD drop. Window, bins, grid, ROI, rate and integration are
all runtime and need no re-flash.

## Saved captures

A run writes `data.npz` alongside `metadata.json` and the resolved
`config.yaml`.

| array | shape | dtype |
|---|---|---|
| `histograms` | `(n_frames, H, W, num_bins)` | float32 |
| `ambient` | `(n_frames, H, W)` | float32 |
| `index`, `device_index` | `(n_frames,)` | int64 |
| `timestamp` | `(n_frames,)` | float64 (host unix time) |
| `device_ts_ms` | `(n_frames,)` | int64 (MCU clock at emit) |

Realsense planes are stored only for the frames that carried one, each with an
index array naming those frames: `rgb_bgr` with `rgb_at`, `depth_mm` with
`depth_at`, `ir_left`/`ir_right` with `ir_left_at`/`ir_right_at`. A 20-frame
burst with one attached image stores a single plane, not twenty copies.

```python
from spad_capture.storage import load

meta, frames = load("outputs/<run>/data.npz")
frames[0]["histogram"]     # (H, W, num_bins) float32
frames[0]["ambient"]       # (H, W) float32
meta["sensor_layout"]["bin_centers_mm"]   # physical x-axis, one per bin
```

## CLI

| Config field | Flag |
|---|---|
| `sensor.mode` | `--mode` (zone grid: `4x4` / `8x8`) |
| `capture.mode` | `--cap-mode` (`stream` / `manual`) |
| `sensor.start_mm` | `--start-mm` |
| `sensor.end_mm` | `--end-mm` |
| `sensor.bin_mm` | `--bin-mm` |
| `sensor.zone` | `--zone` (`ROW,COL`) |
| `sensor.roi` | `--roi` |
| `sensor.ranging_frequency_hz` | `--freq` |
| `sensor.integration_time_ms` | `--integ-ms` |
| `sensor.ranging_mode` | `--ranging-mode` |
| `sensor.port` | `--port` |
| `capture.sum_frames` | `--sum` / `--no-sum` |
| `capture.bg_subtract` | `--bg-subtract` |
| `rgb.align_depth` | `--depth-native` (sets `align_depth=false`) |

Common patterns:

```bash
# default config - 4x4, 0-2000 mm, 10 frames
spad st capture

# tighter window, faster ranging, live dashboard
spad st capture --start-mm 200 --end-mm 1200 --freq 20 --viz

# one zone, so the CNH budget buys many more bins
spad st capture --zone 2,2 --end-mm 4000

# manual bursts, summed for display
spad st capture --cap-mode manual -n 20 --sum

# check what a config resolves to before running it
spad st info -c configs/st/8x8.yaml
```
