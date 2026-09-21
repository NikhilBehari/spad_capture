# spad_capture_st firmware — VL53L8CH CNH streamer

STM32 (stm32duino) sketch that configures an ST **VL53L8CH** SPAD ToF sensor in
**CNH** (Compact Normalized Histogram) mode and streams per-zone time
histograms to the host over the ST-LINK USB virtual COM port. It is the minimal
counterpart to spad_center's STM32CubeIDE firmware: the same ST VL53LMZ ULD CNH
configuration and read path, distilled into one Arduino sketch that emits a
compact binary frame format the host `parser.py` reads directly.

```
firmware/vl53l8ch_cnh/
├── vl53l8ch_cnh.ino   # sketch: CNH config + serial protocol + binary streamer
├── platform.h         # Arduino (Wire) porting-layer declarations for the ULD
├── platform.cpp       # Wire-based RdByte/WrByte/RdMulti/WrMulti/WaitMs/SwapBuffer
└── (vendored ULD)     # you drop the ST VL53LMZ ULD inc/ + src/ here — see below
```

---

## Hardware

| | |
|---|---|
| MCU board | **NUCLEO-F401RE** |
| Sensor shield | **X-NUCLEO-53L8A1** (carries the VL53L8CH) |
| Sensor I2C address | `0x52` 8-bit / `0x29` 7-bit (shield default) |
| Host link | USB CDC over **ST-LINK/V2.1** VCP, VID:PID `0483:374b` |
| Serial baud | **921600** (fixed; matches `config.BAUD`) |

Wiring: the shield plugs straight onto the Arduino headers. The sketch uses the
default Arduino `Wire` bus (`D14`/SDA, `D15`/SCL) at 1 MHz (FM+); it falls back
to 400 kHz if your wiring can't sustain FM+ (edit `I2C_CLOCK_HZ` in the sketch).
On power-up the sketch drives the shield's enable lines before talking to the
sensor — `SPI_I2C_N`=PC13 low (selects I2C), `PWR_EN`=PA7 and `LPn`=PB0 high,
`AVDD_EN`=PB1 high (pins per the spad_center reference firmware). It polls
`vl53lmz_check_data_ready()`, so the `INT` line (PA4) is left unused.

---

## Software prerequisites

### 1. stm32duino core

Add the board index and install the STM32 core:

```bash
arduino-cli config init   # once, if you have no arduino-cli config yet
arduino-cli config add board_manager.additional_urls \
  https://github.com/stm32duino/BoardManagerFiles/raw/main/package_stmicroelectronics_index.json
arduino-cli core update-index
arduino-cli core install STMicroelectronics:stm32
```

In the **Arduino IDE**: *File ▸ Preferences ▸ Additional boards manager URLs*,
paste the same URL, then *Boards Manager* ▸ install **"STM32 MCU based boards"**.

### 2. ST VL53LMZ ULD + CNH plugin (vendored into the sketch folder)

The sketch drives the ST **VL53LMZ ULD** directly (same as spad_center) and uses
its **CNH plugin** (`vl53lmz_plugin_cnh.*`, which depends on
`vl53lmz_plugin_motion_indicator.*`). Vendor the ULD into the sketch folder so
the project is self-contained — Arduino compiles every `.c`/`.cpp`/`.h` in the
sketch directory:

```
firmware/vl53l8ch_cnh/
├── vl53l8ch_cnh.ino
├── platform.h          # provided here
├── platform.cpp        # provided here
├── vl53lmz_api.h        vl53lmz_api.c
├── vl53lmz_buffers.h
├── vl53lmz_plugin_cnh.h                 vl53lmz_plugin_cnh.c
├── vl53lmz_plugin_motion_indicator.h    vl53lmz_plugin_motion_indicator.c
└── (other ULD headers the above include)
```

Get the ULD from ST (STSW-IMG023 "VL53LMZ ULD driver") **or** copy the headers +
sources already present in spad_center at:

```
.../spad_center .../drivers/data/vl53l8ch/VL53LMZ_ULD_API/{inc,src}
```

Copy the ULD `inc/*.h` and the `src/*.c` you need (at minimum `vl53lmz_api.c`,
`vl53lmz_plugin_cnh.c`, `vl53lmz_plugin_motion_indicator.c`) flat into
`vl53l8ch_cnh/`. Do **not** copy ST's CubeIDE `Platform/platform.c` — this folder
ships its own Arduino `platform.cpp`/`platform.h` that implement the same ULD
I2C contract on top of `Wire`.

> Decode contract: the CNH histogram is stored exponent/mantissa. The ULD CNH
> plugin returns, per bin, an integer mantissa `p_hist[b]` and a scaler
> `p_hist_scaler[b]`; the sketch expands it to a plain float magnitude as
> `value = p_hist[b] / (1 << p_hist_scaler[b])` (ambient likewise). The host
> therefore receives ready-to-use **float32** values and never sees the
> exponent encoding. This matches spad_center `Core/Src/app.c`.

---

## Compile & flash

### arduino-cli

The board FQBN is `STMicroelectronics:stm32:Nucleo_64` with the part number set
to `NUCLEO_F401RE`. Flashing goes over the on-board ST-LINK (`stlink` upload).

```bash
cd firmware/vl53l8ch_cnh         # the sketch folder (must contain the ULD too)

# compile
arduino-cli compile \
  -b STMicroelectronics:stm32:Nucleo_64 \
  --board-options pnum=NUCLEO_F401RE \
  .

# flash (ST-LINK; the NUCLEO enumerates at /dev/ttyACM0 on Linux)
arduino-cli upload \
  -b STMicroelectronics:stm32:Nucleo_64 \
  --board-options pnum=NUCLEO_F401RE,upload_method=swdMethod \
  -p /dev/ttyACM0 \
  .
```

### Arduino IDE

1. *Tools ▸ Board ▸ STM32 boards groups ▸ **Nucleo-64***.
2. *Tools ▸ Board part number ▸ **Nucleo F401RE***.
3. *Tools ▸ Upload method ▸ **STM32CubeProgrammer (SWD)***.
4. *Tools ▸ Port* → the ST-LINK port (`/dev/ttyACM0`, `COMx`, or `cu.usbmodem…`).
5. Open `vl53l8ch_cnh/vl53l8ch_cnh.ino` (the ULD + `platform.*` must be in the
   same folder), then **Upload**.

After flashing, the host side discovers the board automatically:

```bash
spadst detect          # finds the ST-LINK VCP, runs the handshake, prints the ID
spadst capture --viz   # configure + stream
```

---

## Serial protocol contract

This is the authoritative MCU side of the contract that host `transport.py`
implements. Control messages are ASCII, `\n`-terminated; responses are ASCII
lines **except data frames, which are binary**. Control bytes are only acted on
at frame boundaries, never in the middle of a binary frame.

### Control / responses

| PC → MCU | Meaning | MCU → PC |
|---|---|---|
| `I\n` | identify / is_alive | `STH1 VL53L8CH <maj>.<min> ID=<u8 hex>\n` (e.g. `STH1 VL53L8CH 1.0 ID=01`). `ID` is the byte `vl53lmz_is_alive()` returns, not a part number; `00` means no sensor answered. |
| `C mode sb nb bf freq integ ranging\n` | configure | `OK CFG\n`, or `ERR CFG <reason>\n` |
| `M\n` | start ranging / streaming | `OK START\n`, then binary frames |
| `S\n` | stop ranging | `OK STOP\n` |

`C`-line integers (all derived host-side; **no millimetres on the wire**):

| field | meaning |
|---|---|
| `mode` | `1` = 4×4 (16 zones), `2` = 8×8 (64 zones) |
| `sb` | CNH `start_bin` (native 37.5348 mm one-way bins) |
| `nb` | CNH `num_bins` (after binning) |
| `bf` | CNH `binning_factor` (ULD sub-sample) |
| `freq` | ranging frequency, Hz (1..30; CNH cap is 30 Hz) |
| `integ` | integration time, ms (2-1000; effective in autonomous mode only — ignored in continuous, where the device auto-integrates to the ranging period) |
| `ranging` | `0` = continuous, `1` = autonomous |

Example (4×4, 0..2000 mm, bin 37 mm → bf 2, 53 bins, 15 Hz, 20 ms, continuous):

```
C 1 0 53 2 15 20 0\n
```

`ERR CFG <reason>` codes: `1` freq out of 1..30, `2` integ == 0, `3` num_bins
== 0, `4` binning_factor == 0, `5` over the 6160-byte on-device CNH budget,
plus `parse`/`mode` for a malformed line. The firmware re-checks the on-device
size from `_cnh_calculate_required_memory` (per_buf+20 over agg_cols*agg_rows,
with 4-byte padding), which equals `zones*num_bins*5 + zones*5 + 28` for the full
grid but differs for ROI/sub-grid configs.

### Data frame (binary, little-endian)

One frame, emitted per ready measurement while ranging. This is exactly what
`parser.parse_frame` consumes (`_FRAME_HEADER = struct.Struct("<B4sIIBHHH")`).

```
offset  type            field
------  --------------  -------------------------------------------------
0       u8              START_BYTE (0xAA)
1       4 bytes         MAGIC "STH1"
5       u32             frame_index        (monotonic from MCU)
9       u32             device_ts_ms       (millis() at emit)
13      u8              mode_code          (1 = 4x4, 2 = 8x8)
14      u16             height (H)
16      u16             width  (W)
18      u16             num_bins (B)
20      f32 * (H*W*B)   histogram, zone-major then bin (zone z = r*W + c)
...     f32 * (H*W)     ambient, one per zone (zone-major)
...     u32             crc32 (IEEE/zlib) over bytes [1 .. crc_start)
...     u8              END_BYTE (0x55)
```

The CRC-32 is the reflected IEEE polynomial (`zlib.crc32`) computed over every
byte after `START_BYTE` up to (not including) the CRC field. The host resyncs on
`0xAA` + `STH1`, reads the header, reads `H*W*B + H*W` float32 + the CRC + the
`END_BYTE`, and verifies both the CRC and the trailing `0x55`.

---

## Defaults & customisation

Compile-time defaults at the top of `vl53l8ch_cnh.ino` mirror
`configs/default.yaml` (4×4, start_bin 0, 53 bins, binning_factor 2, 15 Hz,
20 ms, continuous). A `C` command before `M` overrides them at runtime, so you
normally never edit the sketch — `spadst` pushes the resolved config on every
capture. Change the `DEFAULT_*` macros only if you want a different power-on
configuration for standalone (no-host) bring-up.
