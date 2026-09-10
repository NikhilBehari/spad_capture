"""Capture configuration schema for the ST VL53L8CH CNH histogram sensor."""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field, model_validator

from spad_capture.config import RgbConfig, VizConfig, check_backend

# ---------------------------------------------------------------------------
# Constants — the cross-file ABI. Never re-literal these elsewhere.
# ---------------------------------------------------------------------------

NATIVE_BIN_MM: float = 37.5348   # one-way mm per native bin = ULD VL53LMZ_CNH_BIN_WIDTH_MM (~250 ps, clock-fixed); one-way, not round-trip -- do not halve
MEM_BUDGET_BYTES: int = 6160         # on-device CNH persistent buffer hard cap
MAX_BINS_4X4: int = 75               # documented per-mode bin ceilings (budget formula)
MAX_BINS_8X8: int = 18
MAX_FEATURE_LENGTH: int = 255        # uint8 feature_length cap in the ULD
MAX_SUB_SAMPLE: int = 255            # uint8 sum_span (binning_factor) cap in the ULD
MAX_START_BIN: int = 32767          # int16 wire/cast ceiling for start_bin
INTEG_MS_MIN: int = 2               # ULD vl53lmz_set_integration_time_ms lower bound
INTEG_MS_MAX: int = 1000            # ULD vl53lmz_set_integration_time_ms upper bound
BAUD: int = 921600                   # fixed PC <-> MCU serial baud
ST_LINK_VID: int = 0x0483            # ST-LINK USB vendor id
START_BYTE: int = 0xAA              # binary frame sync byte
END_BYTE: int = 0x55               # binary frame terminator
MAGIC: bytes = b"STH1"               # 4-byte stream/handshake magic


def _cnh_device_bytes(nb_agg: int, num_bins: int) -> int:
    """Exact on-device CNH persistent-buffer size in bytes, matching
    _cnh_calculate_required_memory() in vl53lmz_plugin_cnh.c with
    DISABLE_PING_PONG + DISABLE_VARIANCE set (the flags this firmware uses).
      per_buf = 8 + axf*4 + ceil4(axf) + nb_agg*4 + ceil4(nb_agg)   (axf=nb_agg*num_bins)
      total   = per_buf + 20            (single buffer, no variance)
    ceil4(n) = ((3+n)//4)*4 bytes."""
    axf = nb_agg * num_bins
    per_buf = 8 + axf * 4 + ((3 + axf) // 4) * 4 + nb_agg * 4 + ((3 + nb_agg) // 4) * 4
    return per_buf + 20


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class Mode(str, Enum):
    """Zone grid. 4x4 = 16 zones, 8x8 = 64 zones. No other grids exist."""

    GRID_4x4 = "4x4"
    GRID_8x8 = "8x8"

    @property
    def zones(self) -> int:
        return 16 if self is Mode.GRID_4x4 else 64

    @property
    def hw(self) -> tuple[int, int]:
        return (4, 4) if self is Mode.GRID_4x4 else (8, 8)


class RangingMode(str, Enum):
    """VL53L8CH ranging schedule."""

    CONTINUOUS = "continuous"
    AUTONOMOUS = "autonomous"

    @property
    def code(self) -> int:
        """Wire code sent in the firmware ``C`` command (0=continuous, 1=autonomous)."""
        return 0 if self is RangingMode.CONTINUOUS else 1


class CaptureMode(str, Enum):
    """Frame-sequencing strategy."""

    STREAM = "stream"    # continuous until Ctrl-C / duration / num_frames
    MANUAL = "manual"    # burst num_frames per trigger (Enter / dashboard button)


class StorageFormat(str, Enum):
    NPZ = "npz"
    NONE = "none"


# ---------------------------------------------------------------------------
# Sensor
# ---------------------------------------------------------------------------


class SensorConfig(BaseModel):
    """What to capture and how to talk to the device.

    The capture window is expressed in one-way millimetres; the derived bin
    integers live on :class:`Config`.
    """

    mode: Mode = Mode.GRID_4x4

    # Capture window in physical units (one-way mm).
    start_mm: float = Field(default=0.0, description="Window start (one-way mm).")
    end_mm: float = Field(default=2000.0, description="Window end (one-way mm).")
    bin_mm: float = Field(
        default=NATIVE_BIN_MM,
        description="Requested bin width (mm). Rounded to the NEAREST native 37.53 mm "
                    "multiple (>=1x); delivered width may be slightly smaller or larger "
                    "than requested.",
    )

    ranging_frequency_hz: int = Field(default=15, description="CNH constraint: 1..30 Hz.")
    integration_time_ms: int = Field(
        default=20,
        description="Per-frame integration / photon-collection knob, the kIter "
                    "analog (ULD range 2..1000 ms). Honored only in AUTONOMOUS mode; "
                    "in CONTINUOUS mode (the default) the sensor always integrates to "
                    "maximum (the ranging period) and ignores this value -- the ULD "
                    "writes it in both modes, but continuous is always maximum.",
    )
    ranging_mode: RangingMode = RangingMode.CONTINUOUS

    # -- Optional zone / ROI selection (default None => full grid) ----------
    zone: Optional[tuple[int, int]] = Field(
        default=None,
        description="Single output zone as (row, col), e.g. (3, 4). "
                    "Takes precedence over `roi`. None => full grid.",
    )
    roi: Optional[dict] = Field(
        default=None,
        description="Rectangular ROI: {'start_row','start_col','rows','cols'}. "
                    "Used only when `zone` is None. None => full grid.",
    )

    # Serial / connection.
    port: Optional[str] = Field(default=None, description="Serial port. None = auto-detect ST-LINK.")
    baudrate: int = BAUD
    timeout_s: float = 2.0
    init_wait_s: float = Field(default=2.0, description="Wait after MCU reset before handshake.")


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------


class CaptureConfig(BaseModel):
    """Capture-loop behavior."""

    mode: CaptureMode = CaptureMode.STREAM
    num_frames: int = Field(
        default=10,
        description="Stream: stop after N (when duration_s is None). Manual: burst size.",
    )
    duration_s: Optional[float] = Field(
        default=None,
        description="Stream: stop after this many seconds (overrides num_frames).",
    )
    interval_s: float = Field(
        default=0.0,
        description="Stream mode only: sleep between frames (s). Ignored in manual mode.",
    )
    sum_frames: bool = Field(
        default=False,
        description="Manual mode: display the SUM of the burst's frames as one "
                    "histogram (the raw frames are still saved individually).",
    )
    bg_subtract: bool = Field(
        default=False,
        description="Measure an empty-scene background at startup (point at nothing) "
                    "and subtract it from the DISPLAYED histogram; raw frames are still "
                    "saved un-subtracted. Reveals a weak signal under a big interref.",
    )


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


class StorageConfig(BaseModel):
    """Where and how data is persisted.

    Each capture creates ``<root>/<run_dir>/`` containing ``data.npz``,
    ``config.yaml``, and ``metadata.json``.
    """

    format: StorageFormat = StorageFormat.NPZ
    root: Path = Field(default=Path("outputs"), description="Top-level output root.")
    run_dir_template: str = Field(
        default="{timestamp}_{sensor}_{mode}_{name}",
        description="Per-run subfolder name. Keys: timestamp, sensor, mode, "
                    "capture_mode, name. Consecutive underscores collapse; "
                    "trailing ones strip.",
    )
    data_filename: str = Field(default="data", description="Data filename (no extension).")
    save_metadata: bool = True
    save_resolved_config: bool = True


# ---------------------------------------------------------------------------
# Visualization
# ---------------------------------------------------------------------------




class Config(BaseModel):
    """Full run config. Owns the single authoritative mm <-> bin derivation."""

    name: Optional[str] = Field(
        default=None,
        description="Optional run name, embedded in the output folder name.",
    )
    sensor: SensorConfig = SensorConfig()
    capture: CaptureConfig = CaptureConfig()
    storage: StorageConfig = StorageConfig()
    viz: VizConfig = VizConfig(update_hz=15.0)
    rgb: RgbConfig = RgbConfig(width=640, height=480)

    # -- derived bin geometry (the only place this logic lives) -------------

    @property
    def binning_factor(self) -> int:
        """Native bins aggregated per delivered bin (>= 1)."""
        return max(1, round(self.sensor.bin_mm / NATIVE_BIN_MM))

    @property
    def effective_bin_mm(self) -> float:
        """The bin width the device actually delivers (``bin_mm`` is a request)."""
        return self.binning_factor * NATIVE_BIN_MM

    @property
    def start_bin(self) -> int:
        """First native bin of the window."""
        return max(0, round(self.sensor.start_mm / NATIVE_BIN_MM))

    @property
    def num_bins(self) -> int:
        """Number of delivered (aggregated) bins in the window (>= 1)."""
        span_mm = self.sensor.end_mm - self.start_bin * NATIVE_BIN_MM
        return max(1, round(span_mm / self.effective_bin_mm))

    # -- validation: raise on out-of-budget / bad params (never clamp) ------

    @model_validator(mode="after")
    def _validate(self) -> "Config":
        s = self.sensor
        if not (1 <= s.ranging_frequency_hz <= 30):
            raise ValueError(
                f"ranging_frequency_hz={s.ranging_frequency_hz} out of range; "
                f"the VL53L8CH CNH stream supports 1..30 Hz."
            )
        if not (INTEG_MS_MIN <= s.integration_time_ms <= INTEG_MS_MAX):
            raise ValueError(
                f"integration_time_ms={s.integration_time_ms} out of range; the "
                f"VL53L8CH ULD requires {INTEG_MS_MIN}..{INTEG_MS_MAX} ms."
            )
        if s.ranging_mode is RangingMode.AUTONOMOUS:
            period_ms = 1000.0 / s.ranging_frequency_hz
            if s.integration_time_ms > period_ms:
                raise ValueError(
                    f"integration_time_ms={s.integration_time_ms} exceeds the "
                    f"autonomous ranging period ({period_ms:.1f} ms at "
                    f"{s.ranging_frequency_hz} Hz); the ULD requires integration "
                    f"< ranging period (vl53lmz_api.h:571). Lower integration_time_ms "
                    f"or ranging_frequency_hz."
                )
        if s.bin_mm < NATIVE_BIN_MM:
            raise ValueError(f"bin_mm {s.bin_mm} below native bin {NATIVE_BIN_MM} mm.")
        if self.binning_factor > MAX_SUB_SAMPLE:
            raise ValueError(
                f"binning_factor={self.binning_factor} exceeds the device sum_span "
                f"uint8 cap ({MAX_SUB_SAMPLE}); bin_mm={s.bin_mm} mm is too large (it "
                f"truncates on-device). Reduce bin_mm."
            )
        if self.start_bin > MAX_START_BIN:
            raise ValueError(
                f"start_bin={self.start_bin} exceeds the 16-bit signed wire field "
                f"(max {MAX_START_BIN}); start_mm={s.start_mm} mm is too far "
                f"({s.start_mm / 1000:.1f} m one-way). Reduce start_mm."
            )
        if s.end_mm <= s.start_mm:
            raise ValueError(
                f"end_mm ({s.end_mm}) must be greater than start_mm ({s.start_mm})."
            )
        agg = self._agg_params()
        out_zones = agg["agg_cols"] * agg["agg_rows"]
        num_bins = self.num_bins

        # num_bins hard cap: device feature_length is uint8.
        if num_bins > MAX_FEATURE_LENGTH:
            raise ValueError(
                f"num_bins={num_bins} exceeds the device feature_length cap "
                f"({MAX_FEATURE_LENGTH}); the on-sensor histogram is at most "
                f"{MAX_FEATURE_LENGTH} bins. Increase bin_mm or shrink "
                f"[start_mm, end_mm] so num_bins <= {MAX_FEATURE_LENGTH}."
            )

        is_full_grid = (self.sensor.zone is None and self.sensor.roi is None)
        if is_full_grid:
            zones = s.mode.zones                       # 16 | 64  (unchanged path)
            total = zones * num_bins * 5 + zones * 5 + 28
            if total > MEM_BUDGET_BYTES:
                max_bins = MAX_BINS_4X4 if zones == 16 else MAX_BINS_8X8
                raise ValueError(
                    f"CNH budget exceeded for mode={s.mode.value} "
                    f"(zones={zones}, num_bins={num_bins}): computed {total} "
                    f"bytes > {MEM_BUDGET_BYTES} byte limit. Reduce num_bins to "
                    f"<= {max_bins} for this mode (increase bin_mm or shrink "
                    f"[start_mm, end_mm])."
                )
        else:
            total = _cnh_device_bytes(out_zones, num_bins)
            if total > MEM_BUDGET_BYTES:
                # largest num_bins that fits this ROI, then uint8-capped.
                budget_bins = 0
                while _cnh_device_bytes(out_zones, budget_bins + 1) <= MEM_BUDGET_BYTES:
                    budget_bins += 1
                max_bins = min(budget_bins, MAX_FEATURE_LENGTH)
                raise ValueError(
                    f"CNH budget exceeded for ROI "
                    f"{agg['agg_cols']}x{agg['agg_rows']} "
                    f"({out_zones} output zone(s), num_bins={num_bins}): "
                    f"computed {total} bytes > {MEM_BUDGET_BYTES} byte limit. "
                    f"Reduce num_bins to <= {max_bins} for this ROI "
                    f"(increase bin_mm or shrink [start_mm, end_mm])."
                )
        return self

    # -- aggregate-param derivation (zone / roi -> ULD agg map) -------------

    def _agg_params(self) -> dict:
        """Resolve (agg_start_x, agg_start_y, agg_cols, agg_rows) from zone/roi
        or the full grid. ``zone`` takes priority over ``roi``; both default to
        the full grid. Coord map: (row,col) -> (start_y,start_x). Raises on
        out-of-bounds so a bad ROI is rejected host-side before the wire."""
        h, w = self.sensor.mode.hw            # h=rows dim, w=cols dim (4 or 8)
        z, roi = self.sensor.zone, self.sensor.roi
        if z is not None:
            row, col = int(z[0]), int(z[1])
            if not (0 <= row < h and 0 <= col < w):
                raise ValueError(
                    f"zone (row={row}, col={col}) out of bounds for "
                    f"{self.sensor.mode.value} ({h} rows x {w} cols)."
                )
            return {"agg_start_x": col, "agg_start_y": row,
                    "agg_cols": 1, "agg_rows": 1}
        if roi is not None:
            allowed = {"start_row", "start_col", "rows", "cols"}
            extra = set(roi) - allowed
            if extra:
                raise ValueError(
                    f"roi has unknown key(s) {sorted(extra)}; expected {sorted(allowed)}.")
            sr = int(roi.get("start_row", 0))
            sc = int(roi.get("start_col", 0))
            rr = int(roi.get("rows", h))
            cc = int(roi.get("cols", w))
            if not (0 <= sr < h and 0 <= sc < w):
                raise ValueError(
                    f"roi start (row={sr}, col={sc}) out of bounds for "
                    f"{self.sensor.mode.value} ({h}x{w}).")
            if not (1 <= rr <= h - sr and 1 <= cc <= w - sc):
                raise ValueError(
                    f"roi rows={rr}, cols={cc} exceed grid from start "
                    f"(row={sr}, col={sc}); max rows={h-sr}, cols={w-sc}.")
            return {"agg_start_x": sc, "agg_start_y": sr,
                    "agg_cols": cc, "agg_rows": rr}
        return {"agg_start_x": 0, "agg_start_y": 0, "agg_cols": w, "agg_rows": h}

    # -- canonical resolved descriptor (metadata + GUI /meta) ---------------

    def template_vars(self) -> dict[str, str]:
        """Values the storage run-dir template may reference."""
        return {
            "sensor": "st",
            "mode": self.sensor.mode.value,
            "capture_mode": self.capture.mode.value,
        }

    def resolved(self) -> dict:
        """Single canonical descriptor of the resolved capture geometry.

        Stamped into metadata and served to the GUI via ``/meta``. Field
        names are frozen; firmware/GUI consumers rely on them verbatim.
        """
        h, w = self.sensor.mode.hw
        start_mm = self.start_bin * NATIVE_BIN_MM
        eff = self.effective_bin_mm
        zones = self.sensor.mode.zones
        agg = self._agg_params()
        out_h, out_w = agg["agg_rows"], agg["agg_cols"]
        is_full_grid = (self.sensor.zone is None and self.sensor.roi is None)
        budget_bytes = (
            zones * self.num_bins * 5 + zones * 5 + 28 if is_full_grid
            else _cnh_device_bytes(out_h * out_w, self.num_bins)
        )
        return {
            "mode": self.sensor.mode.value,            # "4x4" | "8x8"
            "height": h,                               # sensor grid height (4|8)
            "width": w,                                # sensor grid width  (4|8)
            "zones": zones,                            # 16 | 64
            "output_height": out_h,                    # ROI frame H (== agg_rows)
            "output_width": out_w,                     # ROI frame W (== agg_cols)
            "output_zones": out_h * out_w,
            "agg_start_x": agg["agg_start_x"],         # = col
            "agg_start_y": agg["agg_start_y"],         # = row
            "agg_cols": agg["agg_cols"],
            "agg_rows": agg["agg_rows"],
            "zone": list(self.sensor.zone) if self.sensor.zone is not None else None,
            "num_bins": self.num_bins,
            "start_bin": self.start_bin,
            "binning_factor": self.binning_factor,
            "native_bin_mm": NATIVE_BIN_MM,
            "effective_bin_mm": eff,
            "start_mm": start_mm,
            "end_mm": start_mm + self.num_bins * eff,
            "bin_centers_mm": [start_mm + (i + 0.5) * eff for i in range(self.num_bins)],
            "ranging_frequency_hz": self.sensor.ranging_frequency_hz,
            "integration_time_ms": self.sensor.integration_time_ms,
            "ranging_mode": self.sensor.ranging_mode.value,
            "budget_bytes": budget_bytes,
            "budget_limit": MEM_BUDGET_BYTES,
        }

    def adjustments(self) -> list[str]:
        """Notes wherever the captured config differs from what was requested --
        so the banner and saved metadata record exactly what the device did
        (sub-bin snapping, bin rounding, a knob the mode silently ignores)."""
        s = self.sensor
        notes: list[str] = []
        got_start = self.start_bin * NATIVE_BIN_MM
        if abs(got_start - s.start_mm) > 0.05:
            notes.append(f"start_mm {s.start_mm:g} -> {got_start:.2f} mm "
                         f"(snapped to native bin {self.start_bin})")
        eff = self.effective_bin_mm
        if abs(eff - s.bin_mm) > 0.05:
            notes.append(f"bin_mm {s.bin_mm:g} -> {eff:.2f} mm "
                         f"(binning_factor {self.binning_factor})")
        got_end = got_start + self.num_bins * eff
        if abs(got_end - s.end_mm) > 0.5:
            notes.append(f"end_mm {s.end_mm:g} -> {got_end:.1f} mm "
                         f"({self.num_bins} bins of {eff:.2f} mm)")
        if s.ranging_mode is RangingMode.CONTINUOUS:
            notes.append(f"integration_time_ms {s.integration_time_ms} ms ignored in "
                         f"continuous mode (sensor integrates to the ranging period)")
        if s.zone is not None and s.roi is not None:
            notes.append(f"zone {list(s.zone)} overrides roi (roi ignored)")
        return notes


def load_config(path: Path | str | None) -> Config:
    """Load a YAML config file, or return defaults if path is None."""
    if path is None:
        return Config()
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(f"Config file not found: {p}")
    raw = yaml.safe_load(p.read_text()) or {}
    check_backend(raw, "st", p)
    return Config(**raw)
