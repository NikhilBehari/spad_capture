# Firmware

## Architecture

Two pieces of firmware are involved:

| Layer | Lives in | Loaded by |
|-------|----------|-----------|
| **Arduino sketch.** A resident bridge that listens on the USB CDC serial link, decodes commands from the host, and drives the TMF8828 over I²C. | the Uno's flash memory | `spad flash`, which builds the sketch from `spad_capture/firmware/tmf8828/` with `arduino-cli` and writes it to the Uno. Persists across power cycles. |
| **TMF8828 application image.** The AMS-provided patch the SPAD chip executes. Two variants are compiled into the sketch: one for 8x8 mode (loaded via `e`), one for the predefined zone maps and custom masks (loaded via `E`). | the TMF8828's volatile RAM | the Arduino sketch, automatically. The image is streamed into the chip over I²C whenever the host issues `e` or `E`. |

The host never talks to the TMF8828 directly. All capture-time settings,
the mask payload, the iteration count, the period, and the range mode
flow over serial to the Arduino sketch, which translates them into I²C
transactions.

## Live-tunable parameters

These knobs are issued over serial by `spad capture` from the resolved
configuration. Per-field defaults and YAML semantics live in
[options.md § firmware](options.md#firmware).

| Parameter | YAML field | Serial command | Meaning |
|-----------|------------|----------------|---------|
| Zone layout | `sensor.zone_mode` | `M<id>\n` (predefined maps), `e` (TMF8828 / 8x8), or `U<id><109 bytes>` (custom mask) | Selects one of the 14 predefined SPAD maps, or installs a runtime-uploaded user mask. |
| Range mode | `sensor.range_mode` | `O` (toggle) | `long` (~5 m, ~260 ps/bin) or `short` (~1 m, ~100 ps/bin). |
| Iterations | `firmware.kilo_iterations` | `I<dec>\n` | VCSEL pulses per measurement divided by 1024. |
| Period | `firmware.period_ms` | `P<dec>\n` | Inter-measurement period in milliseconds. |

Commands take effect on the next measurement start (`m`), which `spad
capture` issues after applying the configuration.

## When the sketch must be rebuilt

`spad flash` recompiles `spad_capture/firmware/tmf8828/*` with
`arduino-cli` and uploads to the detected Arduino in one step:

```bash
spad flash
```

Re-running is safe at any time; the sketch is rebuilt from source each
invocation.

Sketch source changes that require a rebuild:

- adding or modifying a serial-dispatcher command,
- changing the compiled-in defaults for period, iterations, or
  `spad_map_id`,
- bundling a new TMF8828 patch image (the AMS firmware blob).

All other configuration (zone layout, custom-mask payload, iterations,
period, range mode) flows over serial at capture time.
