"""Capture configuration schema."""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field, model_validator


# ---------------------------------------------------------------------------
# Sensor
# ---------------------------------------------------------------------------


class ZoneMode(str, Enum):
    """Predefined SPAD layouts. Names follow DS000693 §7.4.1 Figures 30/31.

    FoV values cited in comments come straight from those figures.
    """

    GRID_3x3_NARROW       = "3x3_narrow"        # map 1,  33°x32°
    GRID_3x3_MACRO        = "3x3_macro"         # map 2,  33°x47°
    GRID_3x3_MACRO_V2     = "3x3_macro_v2"      # map 3,  33°x47°
    GRID_4x4_NARROW       = "4x4_narrow"        # map 4,  33°x47° (time-mux)
    GRID_4x4_NARROW_V2    = "4x4_narrow_v2"     # map 5,  33°x47° (time-mux)
    GRID_3x3_WIDE         = "3x3_wide"          # map 6,  41°x52°
    GRID_4x4_WIDE         = "4x4_wide"          # map 7,  41°x52° (time-mux)
    GRID_9_ZONE           = "9_zone"            # map 8,  irregular 9-zone (14x9)
    GRID_9_ZONE_V2        = "9_zone_v2"         # map 9,  variant
    GRID_3x6              = "3x6"               # map 10, 33°x60° (time-mux)
    GRID_3x3_CHECKER      = "3x3_checker"       # map 11, 33°x32° checkerboard
    GRID_3x3_CHECKER_REV  = "3x3_checker_rev"   # map 12, 33°x32° reverse-checkerboard
    GRID_4x4_NARROW_V3    = "4x4_narrow_v3"     # map 13, 33°x42° (time-mux)
    GRID_8x8              = "8x8"               # map 15, 41°x52° (TMF8828 8 sub-captures)
    CUSTOM                = "custom"            # map 14, user-defined SPAD mask (legacy mode)

    @property
    def spad_map_id(self) -> int:
        return _MAP_IDS[self]


# Mapping from ZoneMode to AMS spad_map_id register value.
_MAP_IDS = {
    ZoneMode.GRID_3x3_NARROW:      1,
    ZoneMode.GRID_3x3_MACRO:       2,
    ZoneMode.GRID_3x3_MACRO_V2:    3,
    ZoneMode.GRID_4x4_NARROW:      4,
    ZoneMode.GRID_4x4_NARROW_V2:   5,
    ZoneMode.GRID_3x3_WIDE:        6,
    ZoneMode.GRID_4x4_WIDE:        7,
    ZoneMode.GRID_9_ZONE:          8,
    ZoneMode.GRID_9_ZONE_V2:       9,
    ZoneMode.GRID_3x6:             10,
    ZoneMode.GRID_3x3_CHECKER:     11,
    ZoneMode.GRID_3x3_CHECKER_REV: 12,
    ZoneMode.GRID_4x4_NARROW_V3:   13,
    ZoneMode.GRID_8x8:             15,
    ZoneMode.CUSTOM:               14,
}


class RangeMode(str, Enum):
    """Distance range. LONG: 260.6 ps/bin, 4.41 m. SHORT: 92.5 ps/bin, 1.566 m."""

    LONG = "long"
    SHORT = "short"


class SensorConfig(BaseModel):
    """What to capture and how to talk to the device.

    Firmware-tunable runtime knobs (iterations, period) live in ``firmware``.
    """

    zone_mode: ZoneMode = ZoneMode.GRID_3x3_WIDE
    range_mode: RangeMode = RangeMode.LONG

    # Serial / connection (rarely changed).
    port: Optional[str] = Field(default=None, description="Serial port. None = auto-detect.")
    baudrate: int = 2_000_000
    timeout_s: float = 1.0
    init_wait_s: float = Field(default=1.5, description="Wait for Arduino reset after open.")

    # Calibration: when True, run an in-place factory crosstalk calibration on
    # the active SPAD mask right after configure() and before the first frame.
    # Per DS000693 §7.3 the cal must be done in a dark housing with no target
    # within 40 cm; it adds ~3 seconds of startup latency. Recommended for
    # custom masks; harmless for predefined zone modes (which include
    # firmware-bundled cal tables).
    calibrate: bool = Field(
        default=False,
        description="Run factory crosstalk calibration before capture begins.",
    )


class FirmwareConfig(BaseModel):
    """Runtime knobs forwarded to the Arduino firmware over serial.

    Every field defaults to ``None``. A ``None`` value means no override
    command is sent and the firmware's baked-in default applies.
    """

    kilo_iterations: Optional[int] = Field(
        default=None,
        description="VCSEL pulses per measurement divided by 1024 (uint16). "
                    "Firmware-baked defaults: 10000 for 3x3 maps, 2500 for "
                    "4x4 maps, 10 for 8x8. Override the 8x8 default with a "
                    "higher value (1000+) to raise signal counts.",
    )
    period_ms: Optional[int] = Field(
        default=None,
        description="Inter-measurement period in milliseconds. Set below "
                    "ranging time for maximum throughput.",
    )


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------


class CaptureMode(str, Enum):
    """Frame-sequencing strategy."""

    SEQUENTIAL = "sequential"   # exactly num_frames, then stop
    STREAMING = "streaming"     # continuous until Ctrl-C or duration_s
    TIMED = "timed"             # for exactly duration_s seconds
    MANUAL = "manual"           # live viz, save num_frames frames per trigger


class CaptureConfig(BaseModel):
    """Capture-loop behavior."""

    mode: CaptureMode = CaptureMode.SEQUENTIAL
    num_frames: int = Field(default=10, description="Number of frames (sequential mode).")
    duration_s: Optional[float] = Field(
        default=None, description="Max duration in seconds (streaming/timed)."
    )
    interval_s: float = Field(
        default=0.0,
        description="Sleep between frames (s). 0 = capture as fast as possible.",
    )
    samples_per_frame: int = Field(
        default=1,
        description="Sensor frames to accumulate per output frame (averaging).",
    )

    @model_validator(mode="after")
    def _validate(self) -> "CaptureConfig":
        if self.mode == CaptureMode.TIMED and self.duration_s is None:
            raise ValueError("'timed' capture mode requires capture.duration_s")
        return self


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


class StorageFormat(str, Enum):
    PKL = "pkl"     # one record per frame, append-only pickle
    NPY = "npy"     # single .npy stack at the end
    HDF5 = "h5"     # streamed h5 with /metadata + /frames
    NONE = "none"   # don't save (useful with viz-only mode)


class StorageConfig(BaseModel):
    """Where and how data is persisted.

    Each capture creates ``<root>/<run_dir>/`` containing:
      - ``data.<ext>``      the histograms
      - ``config.yaml``     the exact resolved config used
      - ``metadata.json``   capture metadata (timing, version, etc.)
    """

    format: StorageFormat = StorageFormat.PKL
    root: Path = Field(default=Path("outputs"), description="Top-level output root.")
    run_dir_template: str = Field(
        default="{timestamp}_{sensor}_{zone_mode}_{range_mode}_{name}",
        description="Per-run subfolder name. Available keys: timestamp, sensor, "
                    "zone_mode, range_mode, capture_mode, name. Consecutive underscores "
                    "from empty placeholders are collapsed; trailing ones are stripped.",
    )
    data_filename: str = Field(default="data", description="Data filename inside run dir (no extension).")
    save_metadata: bool = True
    save_resolved_config: bool = True


# ---------------------------------------------------------------------------
# Visualization
# ---------------------------------------------------------------------------


class VizConfig(BaseModel):
    """Optional live web visualization."""

    enabled: bool = Field(default=False, description="Enable localhost live viz.")
    host: str = "127.0.0.1"
    port: int = 8888
    update_hz: float = Field(default=10.0, description="Max viz refresh rate.")


# ---------------------------------------------------------------------------
# RGB (Realsense)
# ---------------------------------------------------------------------------


class RgbConfig(BaseModel):
    """Optional colocated Realsense RGB capture."""

    enabled: bool = Field(default=False, description="Enable Realsense RGB capture.")
    width: int = 848
    height: int = 480
    fps: int = 30
    serial_number: Optional[str] = Field(
        default=None, description="Device serial (None = first device)."
    )
    save_depth: bool = Field(default=False, description="Also save depth frames.")
    align_depth: bool = Field(
        default=True,
        description="Align depth to the color frame; requires the color stream. With "
                    "depth-only capture there is no color target, so depth stays native.",
    )
    ir_left: bool = Field(default=False, description="Capture the left IR image (infrared 1).")
    ir_right: bool = Field(default=False, description="Capture the right IR image (infrared 2).")
    jpeg_quality: int = Field(default=80, description="JPEG quality (1-100) for viz encoding.")

    @property
    def active(self) -> bool:
        """Any Realsense stream wanted."""
        return self.enabled or self.save_depth or self.ir_left or self.ir_right


# ---------------------------------------------------------------------------
# Root
# ---------------------------------------------------------------------------


class Config(BaseModel):
    """Full run config."""

    name: Optional[str] = Field(
        default=None,
        description="Optional name for the run, embedded in the output folder name.",
    )
    sensor: SensorConfig = SensorConfig()
    firmware: FirmwareConfig = FirmwareConfig()
    capture: CaptureConfig = CaptureConfig()
    storage: StorageConfig = StorageConfig()
    viz: VizConfig = VizConfig()
    rgb: RgbConfig = RgbConfig()
    # Loaded lazily. A top-level CustomMask import would create a circular dep.
    mask: Optional[dict] = Field(
        default=None,
        description="User-defined SPAD mask (only used when sensor.zone_mode == 'custom'). "
                    "Schema: {zones: [{id, spads: [[row, col], ...]}], grid: '...'}.",
    )

    @model_validator(mode="after")
    def _custom_needs_mask(self) -> "Config":
        if self.sensor.zone_mode == ZoneMode.CUSTOM and not self.mask:
            raise ValueError(
                "sensor.zone_mode='custom' requires a top-level `mask:` section. "
                "Define zones (e.g. via `mask: {zones: [{id: 1, spads: [[5,8],[5,9]]}]}`) "
                "or use one of the configs in configs/masks/."
            )
        if self.sensor.zone_mode != ZoneMode.CUSTOM and self.mask:
            # Tolerate but warn that the mask is ignored.
            pass
        return self

    def template_vars(self) -> dict[str, str]:
        """Values the storage run-dir template may reference.

        Underscores collide with the folder-name separator, so zone_mode is
        compacted ("3x3_wide" -> "3x3wide") and range_mode is made
        self-documenting ("long" -> "longrange").
        """
        return {
            "sensor": "tmf",
            "zone_mode": self.sensor.zone_mode.value.replace("_", ""),
            "range_mode": f"{self.sensor.range_mode.value}range",
            "capture_mode": self.capture.mode.value,
        }

    def resolved_mask(self):
        """Return a validated CustomMask for the configured mask dict, or None."""
        if not self.mask:
            return None
        from spad_capture.sensors.tmf.mask import CustomMask
        return CustomMask(**self.mask)


def load_config(path: Path | str | None) -> Config:
    """Load a YAML config file, or return defaults if path is None."""
    if path is None:
        return Config()
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(f"Config file not found: {p}")
    raw = yaml.safe_load(p.read_text()) or {}
    return Config(**raw)
