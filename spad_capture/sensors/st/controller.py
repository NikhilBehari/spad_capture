"""Capture orchestrator. Dispatches per-mode loops and handles transport teardown."""

from __future__ import annotations

import queue as _queue
import signal
import sys
import threading
import time
from collections import deque
from dataclasses import replace
from typing import Callable, Optional

import numpy as np
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from spad_capture.sensors.st.config import CaptureMode, Config
from spad_capture.frame import Frame
from spad_capture.sensors.st.parser import FrameError, read_and_parse
from spad_capture.storage import Writer, make_writer
from spad_capture.sensors.st.transport import VL53L8CHTransport

try:
    from spad_capture.rgb import RealsenseCamera
except Exception:  # pyrealsense2 missing -> non-fatal until rgb/ir/depth enabled
    RealsenseCamera = None  # type: ignore

_console = Console()

# Callbacks have signature ``(frame: Frame) -> None``.
FrameCallback = Callable[[Frame], None]

# Corrupt frames (CRC/sync errors from a serial glitch) are dropped and the
# stream resynced; only an unbroken run this long signals a real link failure.
_MAX_CONSEC_BAD = 64


def _install_stop_handlers(stop_flag: dict) -> None:
    """Stop cleanly on Ctrl-C or a kill, so buffered frames still reach disk."""
    def _handler(_sig, _frm):
        stop_flag["stop"] = True
    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)


def run_capture(
    cfg: Config,
    *,
    frame_callback: Optional[FrameCallback] = None,
    writer: Optional[Writer] = None,
    viz_urls: Optional[list[str]] = None,
    trigger_queue: Optional[_queue.Queue] = None,
) -> int:
    """Run the configured capture loop. Returns the number of frames captured.

    Opens the transport as a context manager (detection gate -> open ->
    handshake -> configure -> start_ranging) so teardown always runs. The
    ``trigger_queue`` is consumed only by manual mode; the viz server enqueues
    a trigger when a client posts ``{"type": "capture"}`` and the host
    enqueues one per Enter on stdin.
    """
    stop = {"stop": False}
    _install_stop_handlers(stop)

    own_writer = writer is None

    # Open the Realsense (if any of rgb/depth/ir requested) BEFORE the transport
    # and the writer, so a missing/unimportable camera aborts before any run dir
    # or serial session exists.
    rgb_cam = _open_realsense(cfg)

    try:
        with VL53L8CHTransport(cfg) as transport:
            device = transport.handshake()
            transport.configure(cfg)
            if writer is None:          # create the run dir only after the gate passes
                writer = make_writer(cfg)
            # adjustments record requested vs delivered (window snap, bin rounding).
            writer.update_layout({**cfg.resolved(), "adjustments": cfg.adjustments()})
            _print_banner(cfg, device, rgb_cam, writer, viz_urls)
            _apply_steady_emitter(cfg, rgb_cam)
            transport.start_ranging()

            bg = None
            if cfg.capture.bg_subtract:
                _console.print("[dim]measuring background — point at an empty scene…[/dim]")
                bg = _measure_bg(cfg, transport)
                _console.print("[green]background measured[/green]  (subtracted from the live view)")

            if cfg.capture.mode == CaptureMode.STREAM:
                n_done = _run_stream(cfg, transport, writer, frame_callback, stop, rgb_cam, bg)
            elif cfg.capture.mode == CaptureMode.MANUAL:
                n_done = _run_manual(cfg, transport, writer, frame_callback, stop,
                                     trigger_queue, rgb_cam, bg)
            else:
                raise ValueError(f"Unknown capture mode: {cfg.capture.mode}")
    finally:
        if rgb_cam is not None:
            rgb_cam.close()
        if own_writer and writer is not None:
            writer.close()

    _console.print(
        f"\n[bold green]done[/bold green]  captured {n_done} frames  →  "
        f"[cyan]{writer.run_dir}[/cyan]"
    )
    return n_done


def _open_realsense(cfg: Config):
    """Open the Realsense before anything else, or return None when not requested.

    Raises a clear error if rgb/ir/depth is requested but pyrealsense2 is missing
    or the camera fails to open, so the run aborts before any run dir is created.
    """
    if not cfg.rgb.active:
        return None
    if RealsenseCamera is None:
        raise RuntimeError(
            "Realsense capture requested but pyrealsense2 is not importable. "
            "Install it or disable rgb/ir/depth."
        )
    try:
        return RealsenseCamera(cfg.rgb)
    except Exception as e:
        raise RuntimeError(
            f"Failed to open Realsense camera ({e}). Unplugged, or in use? "
            "Or disable rgb/ir/depth to capture SPAD only."
        ) from e


def _apply_steady_emitter(cfg: Config, rgb_cam) -> None:
    """Set the dot projector to its steady-state policy ONCE before ranging.

    IR-only -> off (plain IR, lit only by the SPAD's 940 nm spot). Depth-only,
    or any RGB/depth stream without IR -> on. When BOTH depth and IR are wanted
    the projector is pulsed per-burst (manual) or left on (stream), so the steady
    state here is off for manual and on for stream; the per-burst toggle owns the
    rest. Never called per ST frame.
    """
    if rgb_cam is None:
        return
    if rgb_cam.pulse_emitter:
        # both depth+ir: manual pulses (off during burst), stream stays on (depth).
        rgb_cam.set_emitter(not cfg.rgb.ir_no_dots and cfg.capture.mode != CaptureMode.MANUAL)
    else:
        # off only for the IR-without-depth case; on otherwise.
        ir_only = (cfg.rgb.ir_left or cfg.rgb.ir_right) and not cfg.rgb.save_depth
        rgb_cam.set_emitter(not ir_only)


def _attach_rgb(frame: Frame, *, color=None, depth=None, ir_left=None,
                ir_right=None, intrinsics=None) -> None:
    """Attach a RealSense result (single planes or a burst's max-held set) onto a frame."""
    frame.rgb_bgr = color
    frame.depth_mm = depth
    frame.ir_left = ir_left
    frame.ir_right = ir_right
    frame.rgb_intrinsics = intrinsics


def _safe_cb(cb: Optional[FrameCallback], frame: Frame) -> None:
    """Dispatch to the live callback, swallowing (and reporting) callback errors."""
    if cb is None:
        return
    try:
        cb(frame)
    except Exception as e:
        _console.print(f"[yellow]frame_callback failed: {e}[/yellow]")


def _sensor_fps(ts_ms) -> float:
    """Exact ST sensor rate (frames/s) from the MCU emit timestamps.

    ``device_ts_ms`` is the firmware's ``millis()`` at emit — the sensor's own
    clock — so this is immune to host/RealSense/viz/serial overhead and reports
    the true ST cadence (which can sit below the requested ranging frequency).
    Averaged over the window: ``(n-1) / span``.
    """
    ts = [t for t in ts_ms if t]
    if len(ts) < 2:
        return 0.0
    span = (ts[-1] - ts[0]) / 1000.0
    return (len(ts) - 1) / span if span > 0 else 0.0


def _sum_frames(frames: list[Frame]) -> Frame:
    """Sum a burst's histograms + ambient into one display Frame (counts ~Nx taller)."""
    hist = np.sum([f.histogram for f in frames], axis=0).astype(np.float32)
    amb = np.sum([f.ambient for f in frames], axis=0).astype(np.float32)
    last = frames[-1]
    return Frame(index=frames[0].index, timestamp=last.timestamp, extras=dict(last.extras),
                 histogram=hist, ambient=amb, device_ts_ms=last.device_ts_ms,
                 device_index=last.device_index)


def _measure_bg(cfg: Config, transport: VL53L8CHTransport,
                n: int = 100) -> Optional[np.ndarray]:
    """Mean per-frame histogram of the current (empty) scene = the background.

    Returns None if stopped before a single frame arrived, so Ctrl-C during the
    measurement ends the capture cleanly instead of raising.
    """
    expected = cfg.resolved()
    acc, got, bad = None, 0, 0
    while got < n:
        try:
            frame = read_and_parse(transport, expected=expected)
        except FrameError as e:
            bad += 1
            if bad >= _MAX_CONSEC_BAD:
                raise FrameError(f"{bad} consecutive corrupt frames; last: {e}") from None
            continue
        if frame is None:                    # clean stop mid-measurement
            return acc / got if got else None
        bad = 0
        acc = frame.histogram.astype(np.float64) if acc is None else acc + frame.histogram
        got += 1
    return acc / got


def _bg_sub(frame: Frame, bg: Optional[np.ndarray], k: int) -> Frame:
    """Display copy of ``frame`` with ``k``x the background removed (raw untouched)."""
    return frame if bg is None else replace(frame, histogram=frame.histogram - bg * k)


def _print_banner(cfg: Config, device: dict, rgb_cam, writer: Writer,
                  viz_urls: Optional[list[str]]) -> None:
    """Render the opening configuration summary."""
    r = cfg.resolved()
    tbl = Table.grid(padding=(0, 2))
    tbl.add_column(style="dim", width=8)
    tbl.add_column()
    dev_id = device.get("device_id")
    dev_str = f"ID=0x{dev_id:02X}" if dev_id is not None else "ID=?"
    tbl.add_row("device",
                f"VL53L8CH  ·  {dev_str}  ·  {cfg.sensor.port or 'auto-detect'}")
    tbl.add_row("sensor",
                f"{r['mode']}  ·  {r['zones']} zones  ·  {r['ranging_frequency_hz']} Hz  ·  "
                f"{r['ranging_mode']}")
    if r["output_zones"] != r["zones"]:
        tbl.add_row("output",
                    f"{r['output_height']}x{r['output_width']}  ·  {r['output_zones']} zone(s) "
                    f"@ row={r['agg_start_y']}, col={r['agg_start_x']}")
    tbl.add_row("window",
                f"{r['start_mm']:.0f}–{r['end_mm']:.0f} mm  ·  {r['num_bins']} bins  "
                f"[dim]({r['effective_bin_mm']:.1f} mm/bin, bf={r['binning_factor']})[/dim]")
    integ_note = ("ignored in continuous; device auto-integrates to the ranging period"
                  if r["ranging_mode"] == "continuous"
                  else "per-frame exposure lever (autonomous)")
    tbl.add_row("integration",
                f"{r['integration_time_ms']} ms  [dim]({integ_note})[/dim]")
    tbl.add_row("budget",
                f"{r['budget_bytes']} / {r['budget_limit']} bytes")
    tbl.add_row("capture",
                f"{cfg.capture.mode.value}"
                + (f"  ·  n={cfg.capture.num_frames}" if cfg.capture.duration_s is None
                   else f"  ·  duration={cfg.capture.duration_s}s"))
    if rgb_cam is not None:
        streams = []
        if cfg.rgb.enabled:
            streams.append("rgb")
        if cfg.rgb.save_depth:
            streams.append("depth")
        # Mirrors the emitter policy above: dots unless turned off, and manual
        # mode drops them between bursts.
        dots = not cfg.rgb.ir_no_dots and cfg.capture.mode != CaptureMode.MANUAL
        for on, nm in ((cfg.rgb.ir_left, "ir-left"), (cfg.rgb.ir_right, "ir-right")):
            if on:
                streams.append(f"{nm} ({'dots' if dots else 'no dots'})")
        tbl.add_row("camera",
                    f"{cfg.rgb.width}x{cfg.rgb.height} @ {cfg.rgb.fps}fps  ·  "
                    + "  ·  ".join(streams) + "  [dim](background thread)[/dim]")
        if rgb_cam.pulse_emitter and cfg.capture.mode == CaptureMode.MANUAL:
            tbl.add_row("projector", "off per burst (IR max-held) · on for the depth shot")
    tbl.add_row("output", f"{cfg.storage.format.value}  →  [cyan]{writer.run_dir}[/cyan]")
    if viz_urls:
        tbl.add_row("viz", "\n".join(f"[link={u}]{u}[/link]" for u in viz_urls))
    notes = cfg.adjustments()
    if notes:
        tbl.add_row("notes", "\n".join(f"[yellow]›[/yellow] {escape(n)}" for n in notes))
    _console.print(Panel(tbl, title="[bold]spad capture st[/bold]", title_align="left",
                         border_style="cyan", padding=(0, 1)))


# ---------------------------------------------------------------------------
# Shared per-frame helper
# ---------------------------------------------------------------------------


def _capture_and_dispatch(
    cfg: Config,
    transport: VL53L8CHTransport,
    writer: Writer,
    cb: Optional[FrameCallback],
    stop: dict,
    *,
    write: bool = True,
    rgb_latest=None,
    dispatch: bool = True,
    fps: float = 0.0,
    bg: Optional[np.ndarray] = None,
    expected: Optional[dict] = None,
) -> Optional[Frame]:
    """Read the next VALID frame, optionally persist it, and dispatch to the callback.

    Corrupt frames (CRC/sync glitches on the high-rate link) are dropped and the
    stream resynced rather than crashing the capture. Returns ``None`` if the
    stop flag is set before a good frame arrives. ``write=False`` is used by
    manual mode for between-burst frames that keep the dashboard fresh but are
    not written to the data file. ``dispatch=False`` suppresses the live callback
    (used by burst-sum mode, which dispatches one summed frame instead).
    ``rgb_latest`` (a buffered ``RgbFrame`` or None) is attached before
    write/dispatch — already pulled by the caller, so this path never touches
    RealSense itself. ``fps`` is the ST capture rate stamped on the frame for the
    dashboard.
    """
    frame: Optional[Frame] = None
    expected = cfg.resolved() if expected is None else expected
    bad = 0
    while not stop["stop"]:
        try:
            frame = read_and_parse(transport, expected=expected)
            break
        except FrameError as e:
            bad += 1
            if bad == 1 or bad % 16 == 0:
                _console.print(f"[yellow]dropped {bad} corrupt frame(s): {e}[/yellow]")
            if bad >= _MAX_CONSEC_BAD:
                raise FrameError(f"{bad} consecutive corrupt frames; last: {e}") from None
    if frame is None:
        return None
    if rgb_latest is not None:
        _attach_rgb(frame, color=rgb_latest.color_bgr, depth=rgb_latest.depth_mm,
                    ir_left=rgb_latest.ir_left, ir_right=rgb_latest.ir_right,
                    intrinsics=rgb_latest.intrinsics)
    frame.fps = fps
    if write:
        writer.write(frame)
    if dispatch:
        _safe_cb(cb, _bg_sub(frame, bg, 1))
    return frame


# ---------------------------------------------------------------------------
# Mode implementations
# ---------------------------------------------------------------------------


def _run_stream(cfg, transport, writer, cb, stop, rgb_cam=None, bg=None) -> int:
    """Continuous capture.

    Stops on Ctrl-C, on ``duration_s`` elapsed (if set), else after
    ``num_frames``. ``duration_s`` wins over ``num_frames`` when present.
    Prints live FPS on a carriage-return line. Each ST frame attaches the
    buffered ``rgb.latest()`` (cheap, non-blocking); the emitter is fixed for
    the whole session per policy and is never toggled here.
    """
    t0 = time.time()
    last_print = t0
    count = 0
    recent = deque(maxlen=16)   # ~1 s of MCU emit timestamps -> exact rolling ST fps
    fps = 0.0
    timed = cfg.capture.duration_s is not None
    expected = cfg.resolved()   # frame-shape contract; constant for the run
    bound = (f"for {cfg.capture.duration_s:g}s" if timed
             else f"{cfg.capture.num_frames} frames")
    _console.print(f"[dim](capturing {bound} · Ctrl-C to stop early)[/dim]")
    while not stop["stop"]:
        if timed:
            if (time.time() - t0) > cfg.capture.duration_s:
                break
        elif count >= cfg.capture.num_frames:
            break
        rgb = rgb_cam.latest() if rgb_cam is not None else None
        # fps carries the rolling rate up to the previous frame (1-frame lag);
        # the rate is from the sensor clock, so RealSense/viz never skew it.
        f = _capture_and_dispatch(cfg, transport, writer, cb, stop, rgb_latest=rgb,
                                  fps=fps, bg=bg, expected=expected)
        if f is None:
            break
        recent.append(f.device_ts_ms)
        fps = _sensor_fps(recent)
        count += 1
        if time.time() - last_print > 1.0:
            _console.print(
                f"  frame {f.index:6d}  peak={f.histogram.max():9.1f}  "
                f"ST fps={fps:5.2f}",
                end="\r",
            )
            last_print = time.time()
        if cfg.capture.interval_s > 0:
            time.sleep(cfg.capture.interval_s)
    return count


def _run_manual(cfg, transport, writer, cb, stop, trigger_queue, rgb_cam=None, bg=None) -> int:
    """Burst capture on demand, mirroring spad_capture's manual mode.

    Captures one initial burst of ``num_frames``, then STOPS ranging so the
    device is idle and the dashboard holds the last burst (nothing streams
    between clicks). Each trigger (Enter, or the dashboard's
    ``{"type": "capture"}``) starts ranging, captures exactly ``num_frames``
    frames, and stops again.

    RealSense is decoupled: the emitter is set ONCE per burst (off so the IR
    accumulates the SPAD spot, never per ST frame), every ST frame does only a
    non-blocking ``rgb.latest()`` (IR max-held, latest RGB kept), then exactly
    ONE ``grab_depth()`` runs after the burst if depth is requested. The whole
    burst's RealSense result is attached to the burst's FIRST frame only, so
    storage holds one RealSense per burst — not N copies.
    """
    burst = max(1, cfg.capture.num_frames)
    triggers: _queue.Queue = trigger_queue or _queue.Queue()
    pulse = rgb_cam is not None and rgb_cam.pulse_emitter

    # Stdin reader thread. Each newline enqueues one trigger.
    def _stdin_loop() -> None:
        if not sys.stdin or not sys.stdin.readable():
            return
        while not stop["stop"]:
            try:
                line = sys.stdin.readline()
            except Exception:
                return
            if line == "":
                return  # EOF
            try:
                triggers.put_nowait(time.time())
            except Exception:
                pass

    threading.Thread(target=_stdin_loop, daemon=True).start()

    _console.print(
        f"[dim](manual mode · {burst}-frame burst per trigger (Enter or the dashboard's "
        f"Capture button) · device idle between bursts · Ctrl-C to stop)[/dim]"
    )

    saved = 0

    expected = cfg.resolved()   # frame-shape contract; constant for the run

    def _do_burst() -> None:
        nonlocal saved
        # Emitter set ONCE for the whole burst (off when pulsing, so IR collects
        # the SPAD spot). NEVER toggled per frame. No-op when nothing changes.
        if pulse:
            rgb_cam.set_emitter(False)

        frames: list[Frame] = []
        ir_left = ir_right = last_color = None
        for _ in range(burst):
            if stop["stop"]:
                break
            # Buffered RealSense read + IR max-hold are cheap and non-blocking;
            # dispatch=False so the burst shows ONE frame (below), not N flashing by.
            rgb = rgb_cam.latest() if rgb_cam is not None else None
            f = _capture_and_dispatch(cfg, transport, writer, cb, stop, expected=expected,
                                      write=True, dispatch=False)
            if f is None:
                break
            if rgb is not None:
                if rgb.ir_left is not None:
                    ir_left = rgb.ir_left if ir_left is None else np.maximum(ir_left, rgb.ir_left)
                if rgb.ir_right is not None:
                    ir_right = rgb.ir_right if ir_right is None else np.maximum(ir_right, rgb.ir_right)
                if rgb.color_bgr is not None:
                    last_color = rgb.color_bgr
            frames.append(f)
            saved += 1

        if not frames:
            return
        # Exact ST burst fps from the sensor's own emit clock (device_ts_ms),
        # not host wall-clock — RealSense/viz/write never skew it.
        fps_st = _sensor_fps([f.device_ts_ms for f in frames])

        # ONE dense depth shot after the burst (emitter pulses on, SPAD idle).
        depth = rgb_cam.grab_depth() if (rgb_cam is not None and cfg.rgb.save_depth) else None

        # Storage: the burst's single RealSense result rides the first (written)
        # frame, so the npz holds one image per burst (not N copies).
        if rgb_cam is not None:
            _attach_rgb(frames[0], color=last_color, depth=depth, ir_left=ir_left,
                        ir_right=ir_right, intrinsics=rgb_cam.intrinsics)

        # Dashboard: one frame per burst — the SUM if requested, else the first —
        # carrying the burst's RealSense and the exact measured ST sensor fps.
        disp = _sum_frames(frames) if cfg.capture.sum_frames else frames[0]
        disp.fps = fps_st
        if cfg.capture.sum_frames and rgb_cam is not None:
            _attach_rgb(disp, color=last_color, depth=depth, ir_left=ir_left,
                        ir_right=ir_right, intrinsics=rgb_cam.intrinsics)
        disp = _bg_sub(disp, bg, len(frames) if cfg.capture.sum_frames else 1)
        _safe_cb(cb, disp)

        kind = "summed" if cfg.capture.sum_frames else "frames"
        _console.print(f"  saved {len(frames)} {kind} · total {saved} · "
                       f"{fps_st:.1f} ST fps · waiting for next trigger")

    # Initial burst (run_capture already started ranging), then pause the device.
    _do_burst()
    transport.stop_ranging()

    while not stop["stop"]:
        try:
            triggers.get(timeout=0.2)
        except _queue.Empty:
            continue
        if stop["stop"]:
            break
        transport.start_ranging()   # resume ranging for this burst
        _do_burst()
        transport.stop_ranging()    # idle again until the next trigger

    return saved


# ---------------------------------------------------------------------------
# Viz wrapper
# ---------------------------------------------------------------------------


def run_with_viz(cfg: Config, capture_fn: Callable[..., int]) -> int:
    """Start a VizServer, wire it as the frame callback + trigger source, run capture.

    Prints the viz URLs up front so the page can be opened while the device is
    still initializing, then runs ``capture_fn(cfg, frame_callback=...,
    viz_urls=..., trigger_queue=...)`` and stops the server on exit.
    """
    from spad_capture.viz.server import VizServer, viz_urls

    # Light up the rgb/depth/ir pane toggles immediately (before the first JPEG),
    # and honor the configured JPEG quality for encoding.
    sensor_meta = {
        **cfg.resolved(),
        "capture_mode": cfg.capture.mode.value,
        "rgb_enabled": cfg.rgb.enabled,
        "depth_enabled": cfg.rgb.save_depth,
        "ir_enabled": cfg.rgb.ir_left or cfg.rgb.ir_right,
    }
    server = VizServer(cfg.viz, sensor_meta=sensor_meta, jpeg_quality=cfg.rgb.jpeg_quality)
    server.start()
    urls = viz_urls(cfg.viz)
    _console.print(Panel(
        "\n".join(f"[link={u}]{u}[/link]" for u in urls),
        title="[bold]live viz[/bold]", title_align="left",
        border_style="cyan", padding=(0, 1),
    ))
    try:
        return capture_fn(
            cfg,
            frame_callback=server.publish,
            viz_urls=urls,
            trigger_queue=server.capture_triggers,
        )
    finally:
        server.stop()
