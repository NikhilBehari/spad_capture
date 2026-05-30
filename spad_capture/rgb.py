"""Optional Realsense RGB camera capture, colocated with the SPAD."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np

from spad_capture.config import RgbConfig


@dataclass
class RgbFrame:
    color_bgr: np.ndarray            # (H, W, 3) uint8
    depth_mm: Optional[np.ndarray]   # (H, W) uint16 in mm, or None
    intrinsics: dict                 # fx, fy, ppx, ppy, model, coeffs


class RealsenseCamera:
    """Thin wrapper around pyrealsense2 streaming a single device."""

    def __init__(self, cfg: RgbConfig):
        import pyrealsense2 as rs  # imported lazily so a missing install is non-fatal until enabled
        self._rs = rs
        self.cfg = cfg

        self._pipe = rs.pipeline()
        rcfg = rs.config()
        if cfg.serial_number:
            rcfg.enable_device(cfg.serial_number)
        rcfg.enable_stream(rs.stream.color, cfg.width, cfg.height, rs.format.bgr8, cfg.fps)
        if cfg.save_depth:
            rcfg.enable_stream(rs.stream.depth, cfg.width, cfg.height, rs.format.z16, cfg.fps)
        profile = self._pipe.start(rcfg)
        self._align = rs.align(rs.stream.color) if cfg.save_depth else None

        cstream = profile.get_stream(rs.stream.color).as_video_stream_profile()
        intr = cstream.get_intrinsics()
        self.intrinsics = {
            "fx": intr.fx, "fy": intr.fy,
            "ppx": intr.ppx, "ppy": intr.ppy,
            "width": intr.width, "height": intr.height,
            "model": str(intr.model),
            "coeffs": list(intr.coeffs),
        }

    def latest(self, timeout_ms: int = 250) -> Optional[RgbFrame]:
        """Pull the most recent frame; returns None on timeout."""
        try:
            frames = self._pipe.wait_for_frames(timeout_ms=timeout_ms)
        except Exception:
            return None
        if self._align is not None:
            frames = self._align.process(frames)
        color = frames.get_color_frame()
        if not color:
            return None
        color_arr = np.asanyarray(color.get_data())
        depth_arr = None
        if self.cfg.save_depth:
            d = frames.get_depth_frame()
            if d:
                depth_arr = np.asanyarray(d.get_data())
        return RgbFrame(color_bgr=color_arr, depth_mm=depth_arr, intrinsics=self.intrinsics)

    def close(self) -> None:
        try:
            self._pipe.stop()
        except Exception:
            pass

    def __enter__(self) -> "RealsenseCamera":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
