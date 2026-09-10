"""Optional Realsense capture, decoupled from the fast ST loop.

A background thread runs ``wait_for_frames`` continuously and stows the latest
``RgbFrame`` under a lock. ``latest()`` returns that buffered frame instantly,
so the ST CNH loop never blocks on RealSense. The emitter is set once per burst
(never per ST frame); IR is max-held across a burst from the buffered frames.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Optional

import numpy as np

from spad_capture.macos import open_with_retry


def _warn(msg: str) -> None:
    print(f"  {msg}", flush=True)


def _require_device(rs, explicit):
    """The camera serial to open, refusing to guess when several are attached.

    Called after the platform shim has released the device, so the enumeration
    it does is the real one.
    """
    serials = [d.get_info(rs.camera_info.serial_number) for d in rs.context().query_devices()]
    if explicit:
        if explicit not in serials:
            raise RuntimeError(f"Realsense {explicit} not attached. Seen: {serials or 'none'}")
        return explicit
    if len(serials) > 1:
        raise RuntimeError(
            f"{len(serials)} Realsense cameras attached: {', '.join(serials)}.\n"
            "  Fix: set rgb.serial_number in the config to choose one."
        )
    return serials[0] if serials else None


_SETTLE_FRAMES = 8         # frames discarded after the projector is toggled
_WARMUP_FRAMES = 5         # frames that must arrive before an open counts as good
_WARMUP_TIMEOUT_MS = 2000  # per warmup frame


@dataclass
class RgbFrame:
    color_bgr: Optional[np.ndarray]  # (H, W, 3) uint8, or None
    depth_mm: Optional[np.ndarray]   # (H, W) uint16 in mm, or None
    intrinsics: dict                       # fx, fy, ppx, ppy, model, coeffs
    ir_left: Optional[np.ndarray] = None   # (H, W) uint8 left IR (infrared 1), or None
    ir_right: Optional[np.ndarray] = None  # (H, W) uint8 right IR (infrared 2), or None


class RealsenseCamera:
    """pyrealsense2 wrapper with a background reader so reads never block the ST loop."""

    def __init__(self, cfg):
        import pyrealsense2 as rs  # lazy: a missing install is non-fatal until enabled
        self._rs = rs
        self.cfg = cfg

        def open_pipeline():
            """Build and start a fresh pipeline. Retried as a unit: a pipeline
            whose start failed cannot be reused."""
            # A fresh context per attempt: librealsense keeps device state on the
            # context, and reusing one that just failed tends to fail again.
            ctx = rs.context()
            pipe = rs.pipeline(ctx)
            rcfg = rs.config()
            serial = _require_device(rs, cfg.serial_number)
            if serial:
                rcfg.enable_device(serial)
            if cfg.enabled:
                rcfg.enable_stream(rs.stream.color, cfg.width, cfg.height, rs.format.bgr8, cfg.fps)
            if cfg.save_depth:
                rcfg.enable_stream(rs.stream.depth, cfg.width, cfg.height, rs.format.z16, cfg.fps)
            if cfg.ir_left:
                rcfg.enable_stream(rs.stream.infrared, 1, cfg.width, cfg.height, rs.format.y8, cfg.fps)
            if cfg.ir_right:
                rcfg.enable_stream(rs.stream.infrared, 2, cfg.width, cfg.height, rs.format.y8, cfg.fps)
            profile = pipe.start(rcfg)
            # A start can succeed and still deliver nothing. Prove the stream
            # inside the retried unit, so a silent pipeline fails the attempt.
            try:
                for _ in range(_WARMUP_FRAMES):
                    pipe.wait_for_frames(timeout_ms=_WARMUP_TIMEOUT_MS)
            except BaseException:
                try:
                    pipe.stop()      # release the device before the next attempt
                except Exception:
                    pass
                raise
            return pipe, profile, ctx

        self._pipe, profile, self._ctx = open_with_retry(open_pipeline, log=_warn)
        self._align = rs.align(rs.stream.color) if (cfg.save_depth and cfg.enabled and cfg.align_depth) else None

        # Dot projector (on the stereo module). Toggled at most once per burst.
        self._depth_sensor = (profile.get_device().first_depth_sensor()
                              if (cfg.save_depth or cfg.ir_left or cfg.ir_right) else None)
        self._emitter_on: Optional[bool] = None   # unknown until forced below

        self.intrinsics = self._read_intrinsics(profile)

        # Background reader owns the pipeline: the only thread that calls
        # wait_for_frames. It keeps one latest frame, replaced under the lock,
        # and bumps a generation counter so emitter settling can wait for fresh
        # frames without ever touching the pipeline itself.
        self._lock = threading.Lock()
        self._fresh = threading.Condition(self._lock)
        self._buf: Optional[RgbFrame] = None
        self._gen = 0
        self._stop = threading.Event()
        self._reader = threading.Thread(target=self._loop, name="realsense", daemon=True)
        self._reader.start()
        # Force a known state; the controller sets per-burst policy from here.
        self.set_emitter(not cfg.ir_no_dots)

    # -- reader thread ------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.is_set():
            frame = self._read(timeout_ms=1000)
            if frame is not None:
                with self._lock:
                    self._buf = frame
                    self._gen += 1
                    self._fresh.notify_all()

    def latest(self) -> Optional[RgbFrame]:
        """Return the most recently buffered frame instantly (non-blocking)."""
        with self._lock:
            return self._buf

    def _wait_fresh(self, n: int, timeout_ms: int) -> None:
        """Block until the reader has produced ``n`` frames past the current one."""
        with self._lock:
            target = self._gen + n
            deadline = timeout_ms / 1000.0
            while self._gen < target:
                if not self._fresh.wait(timeout=deadline):
                    break

    # -- emitter / depth ----------------------------------------------------

    def set_emitter(self, on: bool) -> None:
        """Toggle the projector, settling the stream only on a real change.

        Settling waits for the background reader to flush stale frames rather
        than reading the pipeline here, so the reader stays the sole consumer.
        """
        ds = self._depth_sensor
        if ds is None or not ds.supports(self._rs.option.emitter_enabled) or on == self._emitter_on:
            return
        ds.set_option(self._rs.option.emitter_enabled, 1 if on else 0)
        # Drive laser power too: emitter_enabled alone can read back a value the
        # hardware did not take, which leaves faint dots in a supposedly clean IR.
        if ds.supports(self._rs.option.laser_power):
            rng = ds.get_option_range(self._rs.option.laser_power)
            ds.set_option(self._rs.option.laser_power, rng.max if on else 0.0)
        self._emitter_on = on
        self._wait_fresh(_SETTLE_FRAMES, timeout_ms=200)

    def grab_depth(self) -> Optional[np.ndarray]:
        """Projector on (settling if needed), then one fresh dense depth frame (mm)."""
        self.set_emitter(True)
        self._wait_fresh(1, timeout_ms=300)   # ensure the depth is post-settle
        fr = self.latest()
        return fr.depth_mm if fr is not None else None

    @property
    def pulse_emitter(self) -> bool:
        """Depth and IR together: the projector toggles per burst, on for depth,
        off for the IR image."""
        return self.cfg.save_depth and (self.cfg.ir_left or self.cfg.ir_right)

    # -- internals ----------------------------------------------------------

    def _read(self, timeout_ms: int) -> Optional[RgbFrame]:
        """Pull and unpack one frameset; returns None on timeout/empty/transient error.

        The ENTIRE body is guarded: pyrealsense2 can raise on the unpack calls
        (get_data / align.process), not just wait_for_frames, and an uncaught
        error here would kill the sole reader thread and silently freeze capture.
        """
        try:
            frames = self._pipe.wait_for_frames(timeout_ms=timeout_ms)
            # IR is read from the raw frameset; alignment targets only depth.
            ir_left_arr = ir_right_arr = None
            if self.cfg.ir_left:
                f = frames.get_infrared_frame(1)
                if f:
                    ir_left_arr = np.asanyarray(f.get_data())
            if self.cfg.ir_right:
                f = frames.get_infrared_frame(2)
                if f:
                    ir_right_arr = np.asanyarray(f.get_data())
            if self._align is not None:
                frames = self._align.process(frames)
            color_arr = None
            if self.cfg.enabled:
                c = frames.get_color_frame()
                if c:
                    color_arr = np.asanyarray(c.get_data())
            depth_arr = None
            if self.cfg.save_depth:
                d = frames.get_depth_frame()
                if d:
                    depth_arr = np.asanyarray(d.get_data())
        except Exception:
            return None
        if color_arr is None and depth_arr is None and ir_left_arr is None and ir_right_arr is None:
            return None
        return RgbFrame(color_bgr=color_arr, depth_mm=depth_arr, ir_left=ir_left_arr,
                        ir_right=ir_right_arr, intrinsics=self.intrinsics)

    def _read_intrinsics(self, profile) -> dict:
        """Intrinsics from whichever stream is active (color preferred)."""
        rs = self._rs
        if self.cfg.enabled:
            sp = profile.get_stream(rs.stream.color)
        elif self.cfg.save_depth:
            sp = profile.get_stream(rs.stream.depth)
        elif self.cfg.ir_left:
            sp = profile.get_stream(rs.stream.infrared, 1)
        else:
            sp = profile.get_stream(rs.stream.infrared, 2)
        intr = sp.as_video_stream_profile().get_intrinsics()
        return {
            "fx": intr.fx, "fy": intr.fy,
            "ppx": intr.ppx, "ppy": intr.ppy,
            "width": intr.width, "height": intr.height,
            "model": str(intr.model),
            "coeffs": list(intr.coeffs),
        }

    # -- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        self._stop.set()
        if getattr(self, "_reader", None) is not None:
            self._reader.join(timeout=2.0)
        try:
            self._pipe.stop()
        except Exception:
            pass

    def __enter__(self) -> "RealsenseCamera":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
