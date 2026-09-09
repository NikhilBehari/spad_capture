"""The frame contract shared by every sensor backend."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np


@dataclass
class Frame:
    """One captured frame: a per-zone histogram plus optional colocated camera planes.

    `histogram` keeps each backend's own dtype: TMF counts are int32, ST CNH
    magnitudes float32. Fields below it are set only by the backends that report
    them, so neither backend carries the other's fields.
    """
    index: int
    timestamp: float
    histogram: np.ndarray                       # (H, W, num_bins)
    ambient: Optional[np.ndarray] = None        # (H, W) per-zone ambient, if reported
    device_ts_ms: int = 0                       # device clock at emit, 0 if unused
    device_index: int = 0                       # device-side counter, resets each run
    fps: float = 0.0                            # measured sensor rate
    rgb_bgr: Optional[np.ndarray] = None        # (Hc, Wc, 3) uint8
    depth_mm: Optional[np.ndarray] = None       # (Hc, Wc) uint16
    ir_left: Optional[np.ndarray] = None        # (Hc, Wc) uint8
    ir_right: Optional[np.ndarray] = None       # (Hc, Wc) uint8
    rgb_intrinsics: Optional[dict] = None       # fx/fy/ppx/ppy/etc
    extras: dict = field(default_factory=dict)  # backend fields carried into metadata

    @property
    def shape(self) -> tuple[int, int, int]:
        return self.histogram.shape
