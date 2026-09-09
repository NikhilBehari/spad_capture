"""TMF8828 SPAD driver.

Serial protocol commands (per the bundled firmware sketch):
    h: handshake     d: reset           o: toggle 8821<->8828 mode
    e: load 8828 fw  E: load 882x fw    c: cycle to next preset config
    O: toggle range  z: histogram on    m: start            s: stop
    f: factory calib l: load calib
"""

from __future__ import annotations

import glob
import queue
import sys
import threading
import time
from typing import Optional

import numpy as np
import serial

from spad_capture.config import FirmwareConfig, RangeMode, SensorConfig, ZoneMode
from spad_capture.frame import Frame

# Per-map output geometry + sub-capture count.
# height, width: zone grid shape returned to caller.
# num_subcaptures, channels_per_subcap: how the firmware streams the histograms.
_ZONE_INFO = {
    ZoneMode.GRID_3x3_NARROW:      dict(height=3, width=3, num_subcaptures=1, channels_per_subcap=[9]),
    ZoneMode.GRID_3x3_MACRO:       dict(height=3, width=3, num_subcaptures=1, channels_per_subcap=[9]),
    ZoneMode.GRID_3x3_MACRO_V2:    dict(height=3, width=3, num_subcaptures=1, channels_per_subcap=[9]),
    ZoneMode.GRID_4x4_NARROW:      dict(height=4, width=4, num_subcaptures=2, channels_per_subcap=[8, 8]),
    ZoneMode.GRID_4x4_NARROW_V2:   dict(height=4, width=4, num_subcaptures=2, channels_per_subcap=[8, 8]),
    ZoneMode.GRID_3x3_WIDE:        dict(height=3, width=3, num_subcaptures=1, channels_per_subcap=[9]),
    ZoneMode.GRID_4x4_WIDE:        dict(height=4, width=4, num_subcaptures=2, channels_per_subcap=[8, 8]),
    ZoneMode.GRID_9_ZONE:          dict(height=3, width=3, num_subcaptures=1, channels_per_subcap=[9]),
    ZoneMode.GRID_9_ZONE_V2:       dict(height=3, width=3, num_subcaptures=1, channels_per_subcap=[9]),
    ZoneMode.GRID_3x6:             dict(height=3, width=6, num_subcaptures=2, channels_per_subcap=[9, 9]),
    ZoneMode.GRID_3x3_CHECKER:     dict(height=3, width=3, num_subcaptures=1, channels_per_subcap=[9]),
    ZoneMode.GRID_3x3_CHECKER_REV: dict(height=3, width=3, num_subcaptures=1, channels_per_subcap=[9]),
    ZoneMode.GRID_4x4_NARROW_V3:   dict(height=4, width=4, num_subcaptures=2, channels_per_subcap=[8, 8]),
    ZoneMode.GRID_8x8:             dict(height=8, width=8, num_subcaptures=8, channels_per_subcap=[8] * 8),
    # CUSTOM is filled in at sensor init from the resolved mask.
}

# FoV per zone_mode in (fovx_deg, fovy_deg) for the full grid.
# Values are taken directly from DS000693 §7.4.1 Figures 30/31 (the per-map
# overlays). Maps 8/9 are not pictured; their FoV is inferred from matching
# 14x9 SPAD area (same as maps 2-5).
_ZONE_FOV = {
    ZoneMode.GRID_3x3_NARROW:      (33.0, 32.0),
    ZoneMode.GRID_3x3_MACRO:       (33.0, 47.0),
    ZoneMode.GRID_3x3_MACRO_V2:    (33.0, 47.0),
    ZoneMode.GRID_4x4_NARROW:      (33.0, 47.0),
    ZoneMode.GRID_4x4_NARROW_V2:   (33.0, 47.0),
    ZoneMode.GRID_3x3_WIDE:        (41.0, 52.0),
    ZoneMode.GRID_4x4_WIDE:        (41.0, 52.0),
    ZoneMode.GRID_9_ZONE:          (33.0, 47.0),
    ZoneMode.GRID_9_ZONE_V2:       (33.0, 47.0),
    ZoneMode.GRID_3x6:             (33.0, 60.0),
    ZoneMode.GRID_3x3_CHECKER:     (33.0, 32.0),
    ZoneMode.GRID_3x3_CHECKER_REV: (33.0, 32.0),
    ZoneMode.GRID_4x4_NARROW_V3:   (33.0, 42.0),
    ZoneMode.GRID_8x8:             (41.0, 52.0),
    ZoneMode.CUSTOM:               (44.0, 48.0),   # max user-area FoV (matches map 6 wide)
}

# Bin timing measured against a ruler, not AMS-published. ``bin_s`` is
# round-trip time per bin, ``bin_mm`` the one-way distance it covers.
# Long range takes its span as exactly 5 m over the 128 bins.
_TIMING = {
    RangeMode.LONG:  {"bin_s": 260.6e-12, "bin_mm": 39.06},
    RangeMode.SHORT: {"bin_s": 92.5e-12,  "bin_mm": 13.864},
}
# Range zero: the reference pulse sits at this bin, so bins below it are
# before the target and the usable span is NUM_BINS - ZERO_BIN.
ZERO_BIN = 15.04

NUM_BINS = 128
_SKIP_FIELDS = 3   # firmware row prefix: "#Raw,Count,Idx,..."


# ---------------------------------------------------------------------------
# Port discovery
# ---------------------------------------------------------------------------


def find_arduino_port() -> str:
    """Locate the Arduino serial port across platforms."""
    if sys.platform.startswith("linux"):
        ports = sorted(glob.glob("/dev/ttyACM*"))
    elif sys.platform == "darwin":
        ports = sorted(p for p in glob.glob("/dev/cu.*") if "usbmodem" in p)
    elif sys.platform == "win32":
        import serial.tools.list_ports as lp
        ports = [
            p.device for p in lp.comports()
            if any(d in (p.description or "") for d in ("Arduino", "USB Serial Device"))
        ]
    else:
        ports = []
    if not ports:
        raise RuntimeError("No Arduino serial port found. Pass --port or set sensor.port in config.")
    return ports[0]


# ---------------------------------------------------------------------------
# Sensor
# ---------------------------------------------------------------------------


class TMF8828Sensor:
    """Thread-safe TMF8828 driver, capture-only.

    Use as a context manager::

        with TMF8828Sensor(config) as spad:
            frame = spad.capture()
    """

    def __init__(self, config: SensorConfig, firmware: Optional[FirmwareConfig] = None,
                 mask=None):
        self.config = config
        self.firmware = firmware or FirmwareConfig()
        self.mask = mask  # spad_capture.sensors.tmf.mask.CustomMask or None
        if config.zone_mode == ZoneMode.CUSTOM:
            if mask is None:
                raise ValueError("zone_mode='custom' requires a mask= argument.")
            from spad_capture.sensors.tmf.mask import validate as _validate_mask
            v = _validate_mask(mask)
            if not v.ok:
                raise ValueError("Custom mask invalid:\n  - " + "\n  - ".join(v.errors))
            # Time-multiplexed custom masks (map_id=15) stream 2 sub-captures
            # of 9 channels each. Single-shot custom masks (map_id=14) stream
            # 1 sub-capture of 9 channels.
            n_zones = len(v.resolved)
            if v.time_multiplexed:
                # In time-mux, each sub-cap is a separate stream of channels 0..9.
                # Resolved order: sub-cap 0 zones first, sub-cap 1 zones second.
                # _resolved_channels stores (sub_capture, channel) pairs so
                # _assemble_frame can pull from the right sub_buffer.
                self._info = dict(
                    height=1, width=n_zones, num_subcaptures=2,
                    channels_per_subcap=[9, 9],
                )
                self._resolved_channels = tuple(
                    (rz.sub_capture, rz.channel) for rz in v.resolved
                )
                self._custom_map_id = 15
            else:
                self._info = dict(
                    height=1, width=n_zones, num_subcaptures=1,
                    channels_per_subcap=[9],
                )
                self._resolved_channels = tuple(
                    (0, rz.channel) for rz in v.resolved
                )
                self._custom_map_id = 14
            self._mask_resolved = v
        else:
            self._info = _ZONE_INFO[config.zone_mode]
            self._resolved_channels = None
            self._mask_resolved = None
        self.height: int = self._info["height"]
        self.width: int = self._info["width"]
        self.num_subcaptures: int = self._info["num_subcaptures"]
        self.channels_per_subcap: list[int] = self._info["channels_per_subcap"]
        self.fov_x, self.fov_y = _ZONE_FOV[config.zone_mode]
        self.bin_width_s = _TIMING[config.range_mode]["bin_s"]
        self.bin_mm = _TIMING[config.range_mode]["bin_mm"]
        # Build resolved_zones: the per-zone SPAD coverage on the 12x18
        # silicon. Same shape for predefined and custom modes so the viz
        # and metadata consumers do not need to special-case either.
        resolved_zones: list[dict] = []
        if config.zone_mode == ZoneMode.CUSTOM:
            for i, rz in enumerate(self._mask_resolved.resolved):
                resolved_zones.append({
                    "output_index": i,
                    "zone_id": rz.zone_id,                  # 0 = dummy pixel
                    "channel": rz.channel,
                    "is_dummy": rz.is_dummy,
                    "spads": [list(s) for s in rz.spads],
                    "output_pos": [0, i],                   # custom is always (1, N)
                    "sub_capture": rz.sub_capture,
                })
        else:
            from spad_capture.sensors.tmf.predefined_layouts import resolved_zones_for
            pre_zones = resolved_zones_for(config.zone_mode.value)
            for i, z in enumerate(pre_zones):
                resolved_zones.append({
                    "output_index": i,
                    "zone_id": z["zone_id"],
                    "channel": z["channel"],
                    "is_dummy": False,
                    "spads": [list(s) for s in z["spads"]],
                    "output_pos": list(z["output_pos"]),
                    "sub_capture": z.get("sub_capture", 0),
                })

        n_user_zones = sum(1 for z in resolved_zones if not z["is_dummy"])
        n_dummy = sum(1 for z in resolved_zones if z["is_dummy"])

        # Spatial-layout descriptor. Persisted into metadata.json so
        # downstream readers can interpret the histogram array: which
        # output_index belongs to which zone, channel, sub-capture, and
        # which SPADs back it. Static per-map facts (FoV, bin width, SPAD
        # area) live in the library, not in this dict.
        if config.zone_mode == ZoneMode.CUSTOM:
            effective_map_id = self._custom_map_id
        else:
            effective_map_id = config.zone_mode.spad_map_id
        self.layout_info: dict = {
            "zone_mode": config.zone_mode.value,
            "spad_map_id": effective_map_id,
            "range_mode": config.range_mode.value,
            "output_shape": [self.height, self.width],
            "num_user_zones": n_user_zones,
            "num_dummy_pixels": n_dummy,
            "resolved_zones": resolved_zones,
            "stream_internal": {
                "num_subcaptures": self.num_subcaptures,
                "histograms_per_subcapture": list(self.channels_per_subcap),
            },
            # Everything needed to put a histogram on a distance axis.
            "timing": {
                "num_bins": NUM_BINS,
                "bin_width_s": self.bin_width_s,
                "bin_mm": self.bin_mm,
                "zero_bin": ZERO_BIN,
                "max_range_mm": round((NUM_BINS - ZERO_BIN) * self.bin_mm, 1),
            },
        }
        if config.zone_mode == ZoneMode.CUSTOM:
            v = self._mask_resolved
            self.layout_info["custom_mask"] = {
                "mode_used": "time-multiplexed (map_id=15)" if v.time_multiplexed
                              else "single-shot (map_id=14)",
            }

        port = config.port or find_arduino_port()
        self._serial = serial.Serial(port, config.baudrate, timeout=config.timeout_s)

        # Wait for Arduino auto-reset after USB open.
        time.sleep(config.init_wait_s)
        self._drain()

        # Configure the firmware synchronously (no reader thread yet so the
        # handshake reads are blocking and won't lose data to a queue).
        self._configure()

        # Reader thread takes over after configure; it pushes complete histogram
        # rows into a queue that capture() pulls from.
        self._line_q: queue.Queue[bytes] = queue.Queue(maxsize=10_000)
        self._stop = threading.Event()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

        self._frame_index = 0

    # ---------- protocol primitives ----------

    def _drain(self) -> None:
        while self._serial.in_waiting:
            self._serial.read(self._serial.in_waiting)

    def _write(self, data: str) -> None:
        self._serial.write(data.encode("utf-8"))

    def _send_and_wait(self, cmd: str, *, settle: float = 0.1, max_quiet: float = 0.3) -> str:
        """Write cmd; wait until firmware has been quiet for ``max_quiet`` seconds.
        Returns the response text the firmware emitted between this write and
        the quiet period. Used to parse e.g. ``#Conf,Period=...`` lines for the
        actual applied configuration values."""
        self._write(cmd)
        last_data_t = time.time()
        buf = b""
        while True:
            if self._serial.in_waiting:
                buf += self._serial.read(self._serial.in_waiting)
                last_data_t = time.time()
            else:
                if time.time() - last_data_t > max_quiet:
                    return buf.decode("utf-8", errors="replace")
                time.sleep(settle / 2)

    @staticmethod
    def _parse_conf(text: str) -> dict:
        """Parse the firmware's '#Conf,Period=XXms,KIter=NN SPAD=N Pers=N' line.

        Returns the LAST seen #Conf line's parsed values (period_ms,
        kilo_iterations, spad_map_id, persistence) as ints. Multiple #Conf
        lines may appear; the last one reflects the final applied state."""
        import re
        out: dict = {}
        for line in text.splitlines():
            if not line.startswith("#Conf"):
                continue
            m_p = re.search(r"Period=(\d+)\s*ms", line)
            m_k = re.search(r"KIter=(\d+)", line)
            m_s = re.search(r"SPAD=(\d+)", line)
            m_x = re.search(r"Pers=(\d+)", line)
            if m_p: out["period_ms"] = int(m_p.group(1))
            if m_k: out["kilo_iterations"] = int(m_k.group(1))
            if m_s: out["spad_map_id"] = int(m_s.group(1))
            if m_x: out["persistence"] = int(m_x.group(1))
        return out

    def _read_loop(self) -> None:
        """Continuously read newline-delimited bytes off the serial port."""
        ser = self._serial
        while not self._stop.is_set():
            try:
                if ser.in_waiting:
                    line = ser.readline()
                    if line:
                        try:
                            self._line_q.put(line, block=False)
                        except queue.Full:
                            pass  # drop on overflow
                else:
                    time.sleep(0.001)
            except Exception:
                if self._stop.is_set():
                    return
                time.sleep(0.01)

    # ---------- custom-mask upload ----------

    def _run_factory_calibration(self) -> tuple[int, Optional[bytes]]:
        """Run factory crosstalk calibration in-place for the currently-active
        SPAD mask (firmware 'Q' command). Returns ``(cal_status, raw_188_bytes)``
        where ``cal_status`` is the device's CALIBRATION_STATUS reg
        (0x00 = matched & loaded, 0x31 = no cal, 0x32 = mismatch, 49+ = other
        device errors during cal), and ``raw_188_bytes`` is the cal payload
        as emitted by the firmware (None on failure).

        Per DS000693 §7.3 the cal must run in a dark housing with no target
        within 40 cm. Runs at 4000 kIter on the device, takes a few seconds.
        """
        self._drain()
        self._serial.write(b"Q")
        deadline = time.time() + 30.0
        buf = b""
        cal_bytes = None
        cal_status_after_cal = -1
        while time.time() < deadline:
            if self._serial.in_waiting:
                buf += self._serial.read(self._serial.in_waiting)
                if b"#Q ok" in buf or b"#Err Q" in buf:
                    break
            else:
                time.sleep(0.02)
        text = buf.decode("utf-8", errors="replace")
        for line in text.splitlines():
            if line.startswith("#CAL_STATUS="):
                try:
                    cal_status_after_cal = int(line.split("=", 1)[1])
                except ValueError:
                    pass
            elif line.startswith("#CAL,"):
                tokens = line.split(",")[1:-1]
                vals = [int(t) for t in tokens if t.strip().isdigit()]
                if len(vals) == 188:
                    cal_bytes = bytes(vals)
            elif line.startswith("#Err Q"):
                raise RuntimeError(f"Calibration failed: {line.strip()}")
        return cal_status_after_cal, cal_bytes

    def _wait_for(self, ok_marker: bytes, err_marker: bytes, timeout: float, label: str) -> None:
        """Drain serial until ok_marker (success) or err_marker (failure) appears."""
        deadline = time.time() + timeout
        buf = b""
        while time.time() < deadline:
            if self._serial.in_waiting:
                buf += self._serial.read(self._serial.in_waiting)
                if ok_marker in buf:
                    return
                if err_marker in buf:
                    msg = buf.decode("utf-8", errors="replace").splitlines()
                    err = next((l for l in msg if err_marker.decode() in l), "unknown")
                    raise RuntimeError(f"{label} rejected: {err.strip()}")
            else:
                time.sleep(0.02)
        raise TimeoutError(f"No firmware ack after {label} ({timeout}s).")

    def _upload_custom_mask(self) -> None:
        """Upload a custom mask to the device.

        For single-shot custom masks (rows 0..5 only), sends one 'U' command
        with map_id=14 + 109-byte payload to write SPAD-1 page.

        For time-multiplexed custom masks (any zone in rows 6..9), sends two
        commands: 'U' (map_id=15 + sub-cap-0 payload to SPAD-1 page) followed
        by 'V' (sub-cap-1 payload to SPAD-2 page). The device alternates
        between the two pages during ranging.

        Mask validity is checked at __init__; this step only serializes and sends.
        """
        from spad_capture.sensors.tmf.mask import to_bytes, to_bytes_split
        if self._custom_map_id == 14:
            payload = to_bytes(self.mask)
            assert len(payload) == 109
            self._drain()
            self._serial.write(b"U" + bytes([14]) + payload)
            self._wait_for(b"#U ok", b"#Err U", 3.0, "Custom-mask 'U' upload")
        else:
            # Time-multiplexed: two pages.
            payload1, payload2 = to_bytes_split(self.mask)
            assert len(payload1) == 109 and len(payload2) == 109
            self._drain()
            self._serial.write(b"U" + bytes([15]) + payload1)
            self._wait_for(b"#U ok", b"#Err U", 3.0,
                           "Time-mux custom-mask 'U' (sub-cap 0) upload")
            self._drain()
            self._serial.write(b"V" + payload2)
            self._wait_for(b"#V ok", b"#Err V", 3.0,
                           "Time-mux custom-mask 'V' (sub-cap 1) upload")

    # ---------- configure ----------

    def _configure(self) -> None:
        """Bring the firmware into the desired mode and apply optional overrides."""
        # Collect every command response; the final #Conf line emitted by
        # the 'Y' command below carries the device-confirmed values.
        applied_buf: list[str] = []
        def _track(cmd: str, **kw) -> None:
            applied_buf.append(self._send_and_wait(cmd, **kw))

        _track("h")
        _track("d")

        if self.config.zone_mode == ZoneMode.GRID_8x8:
            # TMF8828 mode is the only mode that produces 8x8 output.
            _track("e", max_quiet=2.0)
        elif self.config.zone_mode == ZoneMode.CUSTOM:
            # Custom masks require TMF882x (legacy) mode (DS000693 §7.4.1).
            _track("o")
            _track("E", max_quiet=2.0)
            self._upload_custom_mask()
        else:
            # Legacy mode: load TMF882x firmware, then select the map_id.
            _track("o")
            _track("E", max_quiet=2.0)
            map_id = self.config.zone_mode.spad_map_id
            if map_id != 6:
                _track(f"M{map_id}\n")

        if self.config.range_mode == RangeMode.SHORT:
            _track("O")

        # Firmware-side overrides. Only sent when set explicitly so the
        # firmware-baked defaults are otherwise preserved verbatim.
        if self.firmware.kilo_iterations is not None:
            _track(f"I{self.firmware.kilo_iterations}\n")
        if self.firmware.period_ms is not None:
            _track(f"P{self.firmware.period_ms}\n")

        # 'Y' reads back the active #Conf line without changing device state.
        _track("Y")
        applied = self._parse_conf("\n".join(applied_buf))
        if applied:
            self.layout_info["firmware_applied"] = applied

        # Factory crosstalk calibration.
        #
        # Per AN001015 §4.2, calibration is per device and per active SPAD
        # mask. Without a matching cal loaded at startup, the device falls
        # back to a generic internal default and CALIBRATION_STATUS reports
        # 0x31; ranging still works, but histograms include uncompensated
        # VCSEL and optical crosstalk. Pass ``--calibrate`` to run cal at
        # startup (a few seconds; cal lives in device RAM for the session).
        self.calibration_info: dict = {
            "mode": "custom" if self.config.zone_mode == ZoneMode.CUSTOM else "predefined",
            "requested": bool(self.config.calibrate),
            "run": False,
        }
        if self.config.calibrate:
            try:
                status, cal_bytes = self._run_factory_calibration()
                self.calibration_info["run"] = True
                self.calibration_info["status_code"] = int(status)
                self.calibration_info["status"] = {
                    0x00: "matched",
                    0x31: "no cal loaded after run (load failed)",
                    0x32: "mismatch; cal does not fit current mask",
                }.get(status, f"code 0x{status:02X}")
                if cal_bytes is not None:
                    import base64
                    self.calibration_info["cal_page_b64"] = base64.b64encode(cal_bytes).decode("ascii")
                    self.calibration_info["cal_page_bytes"] = len(cal_bytes)
            except Exception as e:
                self.calibration_info["error"] = str(e)

        # Enable histogram dumping and start the measurement stream.
        self._send_and_wait("z")
        self._write("m")
        # Measurement output streams forever; wait for the first row to appear
        # so capture() doesn't trip on an empty queue.
        t0 = time.time()
        while not self._serial.in_waiting and time.time() - t0 < 3.0:
            time.sleep(0.02)

    # ---------- measurement control ----------

    def pause_measurement(self) -> None:
        """Stop the device's frame stream and drain in-flight frames.

        After this returns, ``capture()`` blocks until
        :meth:`resume_measurement` is called.
        """
        self._write("s")
        time.sleep(0.1)
        self._drain()
        try:
            while True:
                self._line_q.get_nowait()
        except queue.Empty:
            pass

    def resume_measurement(self) -> None:
        """Resume the device's frame stream after :meth:`pause_measurement`.

        Waits briefly for the first byte so the next ``capture()`` returns
        a fresh, complete frame.
        """
        self._write("m")
        t0 = time.time()
        while not self._serial.in_waiting and time.time() - t0 < 3.0:
            time.sleep(0.01)

    # ---------- frame parsing ----------

    def capture(self, samples: int = 1, *, timeout_s: float = 30.0) -> Frame:
        """Capture and return one Frame, accumulating ``samples`` raw frames."""
        accumulated = np.zeros((self.height, self.width, NUM_BINS), dtype=np.int64)
        for _ in range(samples):
            accumulated += self._capture_one(timeout_s=timeout_s).astype(np.int64)
        if samples > 1:
            accumulated //= samples
        self._frame_index += 1
        return Frame(
            index=self._frame_index - 1,
            timestamp=time.time(),
            histogram=accumulated.astype(np.int32),
        )

    def _capture_one(self, *, timeout_s: float) -> np.ndarray:
        """Block until exactly one full frame (all sub-captures) is decoded."""
        deadline = time.time() + timeout_s
        sub_buffer: list[np.ndarray] = []     # one ndarray per sub-capture, (channels+1, num_bins)
        current_sub = 0
        active_ch = self.channels_per_subcap[current_sub]
        sub_buffer.append(np.zeros((active_ch + 1, NUM_BINS), dtype=np.int64))
        last_idx = -1
        bad = False

        while time.time() < deadline:
            try:
                raw_line = self._line_q.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                line = raw_line.decode("utf-8").rstrip("\r\n")
            except UnicodeDecodeError:
                continue
            if not line.startswith("#Raw"):
                continue
            row = line.split(",")
            try:
                idx = int(row[_SKIP_FIELDS - 1])
                data = np.array(row[_SKIP_FIELDS:], dtype=np.int32)
            except (IndexError, ValueError, OverflowError):
                bad = True
                continue
            if len(data) != NUM_BINS:
                bad = True
                continue
            if idx != last_idx + 1 and not bad:
                # Out-of-order row; abort this frame.
                bad = True
                continue
            last_idx = idx

            base_idx = idx // 10
            channel = idx % 10
            if 0 <= channel <= active_ch:
                if base_idx == 0:
                    sub_buffer[current_sub][channel] += data
                elif base_idx == 1:
                    sub_buffer[current_sub][channel] += data * 256
                elif base_idx == 2:
                    sub_buffer[current_sub][channel] += data * 256 * 256

            # End-of-sub-capture marker: last channel of base 2.
            if base_idx == 2 and channel == active_ch:
                current_sub += 1
                if current_sub == self.num_subcaptures:
                    return self._assemble_frame(sub_buffer)
                active_ch = self.channels_per_subcap[current_sub]
                sub_buffer.append(np.zeros((active_ch + 1, NUM_BINS), dtype=np.int64))
                last_idx = -1
                bad = False
        raise TimeoutError(f"Timed out after {timeout_s}s waiting for a frame")

    def _assemble_frame(self, sub_buffer: list[np.ndarray]) -> np.ndarray:
        """Reshape sub-captures into (H, W, num_bins)."""
        # Drop channel-0 (reference) row from each sub-capture.
        pieces = [
            sb[1 : self.channels_per_subcap[i] + 1, :]
            for i, sb in enumerate(sub_buffer)
        ]
        combined = np.vstack(pieces)  # (sum_channels, num_bins)

        if self.config.zone_mode == ZoneMode.CUSTOM:
            # _resolved_channels is a tuple of (sub_capture, channel) pairs.
            # Each sub-capture's pieces[i] has shape (channels_per_subcap[i], num_bins)
            # holding channels 1..N (channel 0 = reference, already dropped).
            picked = np.zeros((len(self._resolved_channels), NUM_BINS),
                              dtype=combined.dtype)
            for out_idx, (sub_idx, ch) in enumerate(self._resolved_channels):
                # `pieces[sub_idx][ch - 1]` is the channel-`ch` histogram from
                # sub-capture `sub_idx` (ch=1 is the first non-reference channel).
                picked[out_idx] = pieces[sub_idx][ch - 1]
            return picked.reshape(1, len(self._resolved_channels), NUM_BINS).astype(np.int32)

        if self.config.zone_mode == ZoneMode.GRID_8x8:
            # 8x8 mode streams 64 channels with a non-trivial readout order;
            # the lookup table maps each stream index to its (row, col) in the
            # 8x8 output grid (per the AMS firmware patch image).
            pixel_map = {
                 1:57,  2:61,  3:41,  4:45,  5:25,  6:29,  7: 9,  8:13,
                11:58, 12:62, 13:42, 14:46, 15:26, 16:30, 17:10, 18:14,
                21:59, 22:63, 23:43, 24:47, 25:27, 26:31, 27:11, 28:15,
                31:60, 32:64, 33:44, 34:48, 35:28, 36:32, 37:12, 38:16,
                41:49, 42:53, 43:33, 44:37, 45:17, 46:21, 47: 1, 48: 5,
                51:50, 52:54, 53:34, 54:38, 55:18, 56:22, 57: 2, 58: 6,
                61:51, 62:55, 63:35, 64:39, 65:19, 66:23, 67: 3, 68: 7,
                71:52, 72:56, 73:36, 74:40, 75:20, 76:24, 77: 4, 78: 8,
            }
            spatial = np.zeros((8, 8, NUM_BINS), dtype=combined.dtype)
            for i, src_key in enumerate(pixel_map.keys()):
                target = pixel_map[src_key] - 1
                spatial[target // 8, target % 8, :] = combined[i, :]
            return spatial.astype(np.int32)

        return combined.reshape(self.height, self.width, NUM_BINS).astype(np.int32)

    # ---------- lifecycle ----------

    def close(self) -> None:
        try:
            self._write("s")  # stop measurement
            time.sleep(0.2)
        except Exception:
            pass
        self._stop.set()
        self._reader.join(timeout=2.0)
        try:
            self._serial.close()
        except Exception:
            pass

    def __enter__(self) -> "TMF8828Sensor":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
