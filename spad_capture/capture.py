"""Capture orchestrator. Dispatches per-mode loops and threads the per-frame work."""

from __future__ import annotations

import queue as _queue
import signal
import sys
import threading
import time
from typing import Callable, Optional

from rich.console import Console
from rich.panel import Panel
from rich.progress import (BarColumn, Progress, TaskProgressColumn, TextColumn,
                           TimeElapsedColumn)
from rich.table import Table

from spad_capture.config import CaptureMode, Config
from spad_capture.sensor import Frame, TMF8828Sensor
from spad_capture.storage import Writer, make_writer

try:
    from spad_capture.rgb import RealsenseCamera
except Exception:
    RealsenseCamera = None  # type: ignore

_console = Console()

# Callbacks have signature ``(frame: Frame) -> None``.
FrameCallback = Callable[[Frame], None]


def _install_sigint_handler(stop_flag: dict) -> None:
    def _handler(_sig, _frm):
        stop_flag["stop"] = True
    signal.signal(signal.SIGINT, _handler)


def run_capture(
    cfg: Config,
    *,
    frame_callback: Optional[FrameCallback] = None,
    writer: Optional[Writer] = None,
    viz_urls: Optional[list[str]] = None,
    trigger_queue: Optional[_queue.Queue] = None,
) -> int:
    """Run the configured capture loop. Returns the number of frames captured.

    ``trigger_queue`` is consumed only by ``manual`` mode. The viz server
    enqueues a trigger when a client posts ``{"type": "capture"}``; in
    manual mode the host also enqueues one for every Enter pressed on
    stdin. Each trigger writes ``cfg.capture.num_frames`` frames to disk.
    """
    stop = {"stop": False}
    _install_sigint_handler(stop)

    # Validate the custom mask (if any) BEFORE touching disk so an invalid
    # mask fails without leaving an empty output dir behind.
    if cfg.mask is not None:
        from spad_capture.mask import validate as _validate_mask
        m = cfg.resolved_mask()
        v = _validate_mask(m)
        if not v.ok:
            raise ValueError("Custom mask invalid:\n  - " + "\n  - ".join(v.errors))
        # Custom masks without a matching factory calibration have a baked-in
        # crosstalk peak that suppresses target signal. Warn but proceed.
        if not cfg.sensor.calibrate:
            _console.print(
                "[yellow]heads up[/yellow]  custom mask without [bold]--calibrate[/bold]: "
                "histograms include uncompensated VCSEL crosstalk and counts "
                "will be ~3-4× lower than calibrated. Re-run with "
                "[bold]--calibrate[/bold] (needs dark housing, no target within "
                "40 cm) for accurate signal."
            )

    # Open the RGB camera (if requested) before creating the writer/output dir,
    # so a missing camera aborts before any output file is created.
    rgb_cam = None
    if cfg.rgb.enabled:
        if RealsenseCamera is None:
            raise RuntimeError(
                "rgb.enabled = true but pyrealsense2 is not importable. "
                "Install it or set rgb.enabled = false."
            )
        try:
            rgb_cam = RealsenseCamera(cfg.rgb)
        except Exception as e:
            raise RuntimeError(
                f"Failed to open Realsense camera ({e}). "
                "Disconnect/unplug? Or set rgb.enabled = false to capture SPAD only."
            ) from e

    own_writer = writer is None
    writer = writer or make_writer(cfg)

    try:
        with TMF8828Sensor(cfg.sensor, cfg.firmware, mask=cfg.resolved_mask()) as sensor:
            # Stamp metadata with the device-confirmed layout and the
            # calibration status so each saved capture is self-describing.
            writer.update_layout(getattr(sensor, "layout_info", {}))
            writer.update_calibration(getattr(sensor, "calibration_info", {
                "requested": cfg.sensor.calibrate,
                "run": False,
            }))
            _print_banner(cfg, sensor, rgb_cam, writer, viz_urls)
            ctx = dict(rgb_cam=rgb_cam, trigger_queue=trigger_queue)
            if cfg.capture.mode == CaptureMode.SEQUENTIAL:
                n_done = _run_sequential(cfg, sensor, writer, frame_callback, stop, ctx)
            elif cfg.capture.mode == CaptureMode.STREAMING:
                n_done = _run_streaming(cfg, sensor, writer, frame_callback, stop, ctx)
            elif cfg.capture.mode == CaptureMode.TIMED:
                n_done = _run_timed(cfg, sensor, writer, frame_callback, stop, ctx)
            elif cfg.capture.mode == CaptureMode.MANUAL:
                n_done = _run_manual(cfg, sensor, writer, frame_callback, stop, ctx)
            else:
                raise ValueError(f"Unknown capture mode: {cfg.capture.mode}")
    finally:
        if rgb_cam is not None:
            rgb_cam.close()

    if own_writer:
        writer.close()
    _console.print(f"\n[bold green]done[/bold green]  captured {n_done} frames  →  [cyan]{writer.run_dir}[/cyan]")
    return n_done


def _print_banner(cfg: Config, sensor: TMF8828Sensor, rgb_cam, writer: Writer,
                  viz_urls: Optional[list[str]]) -> None:
    """Render the opening configuration summary."""
    tbl = Table.grid(padding=(0, 2))
    tbl.add_column(style="dim", width=8)
    tbl.add_column()
    tbl.add_row("sensor",
                f"{cfg.sensor.zone_mode.value}  ·  {cfg.sensor.range_mode.value}  ·  "
                f"{cfg.sensor.port or 'auto-detect'}  "
                f"[dim]({sensor.height}x{sensor.width} zones, "
                f"{sensor.num_subcaptures} sub-captures, "
                f"{sensor.bin_width_s*1e12:.0f} ps/bin)[/dim]")
    if cfg.firmware.kilo_iterations is not None or cfg.firmware.period_ms is not None:
        bits = []
        if cfg.firmware.kilo_iterations is not None:
            bits.append(f"{cfg.firmware.kilo_iterations} kIter")
        if cfg.firmware.period_ms is not None:
            bits.append(f"{cfg.firmware.period_ms} ms period")
        tbl.add_row("firmware", "  ·  ".join(bits))
    tbl.add_row("capture",
                f"{cfg.capture.mode.value}"
                + (f"  ·  n={cfg.capture.num_frames}" if cfg.capture.mode == CaptureMode.SEQUENTIAL else "")
                + (f"  ·  duration={cfg.capture.duration_s}s" if cfg.capture.duration_s else ""))
    if rgb_cam is not None:
        d_bit = "  ·  depth" if cfg.rgb.save_depth else ""
        tbl.add_row("rgb", f"{cfg.rgb.width}x{cfg.rgb.height} @ {cfg.rgb.fps}fps{d_bit}")
    tbl.add_row("output", f"{cfg.storage.format.value}  →  [cyan]{writer.run_dir}[/cyan]")
    # The viz URL is printed up front by run_with_viz, before sensor init.
    _console.print(Panel(tbl, title="[bold]spad capture[/bold]", title_align="left",
                         border_style="cyan", padding=(0, 1)))


# ---------------------------------------------------------------------------
# Mode implementations
# ---------------------------------------------------------------------------


def _capture_and_dispatch(
    cfg: Config,
    sensor: TMF8828Sensor,
    writer: Writer,
    cb: Optional[FrameCallback],
    ctx: dict,
    *,
    write: bool = True,
) -> Frame:
    """Capture one frame, attach RGB/depth, optionally persist, and dispatch
    to the live callback. ``write=False`` is used by manual mode for the
    preview frames that should reach the dashboard but not the data file."""
    frame = sensor.capture(samples=cfg.capture.samples_per_frame)
    rgb_cam = ctx.get("rgb_cam")
    if rgb_cam is not None:
        rgb = rgb_cam.latest(timeout_ms=200)
        if rgb is not None:
            frame.rgb_bgr = rgb.color_bgr
            frame.depth_mm = rgb.depth_mm
            frame.rgb_intrinsics = rgb.intrinsics
    if write:
        writer.write(frame)
    if cb is not None:
        try:
            cb(frame)
        except Exception as e:
            _console.print(f"[yellow]frame_callback failed: {e}[/yellow]")
    return frame


def _run_sequential(cfg, sensor, writer, cb, stop, ctx) -> int:
    n = cfg.capture.num_frames
    captured = 0
    with Progress(
        TextColumn("[bold cyan]capturing"),
        BarColumn(),
        TaskProgressColumn(),
        TextColumn("•"),
        TimeElapsedColumn(),
        console=_console,
    ) as bar:
        task = bar.add_task("seq", total=n)
        for _ in range(n):
            if stop["stop"]:
                break
            f = _capture_and_dispatch(cfg, sensor, writer, cb, ctx)
            captured += 1
            bar.update(task, advance=1, description=f"[cyan]frame {f.index} peak={f.histogram.max()}")
            if cfg.capture.interval_s > 0:
                time.sleep(cfg.capture.interval_s)
    return captured


def _run_streaming(cfg, sensor, writer, cb, stop, ctx) -> int:
    t0 = time.time()
    last_print = t0
    count = 0
    _console.print("[dim](streaming · Ctrl-C to stop)[/dim]")
    while not stop["stop"]:
        if cfg.capture.duration_s and (time.time() - t0) > cfg.capture.duration_s:
            break
        f = _capture_and_dispatch(cfg, sensor, writer, cb, ctx)
        count += 1
        if time.time() - last_print > 1.0:
            elapsed = time.time() - t0
            _console.print(
                f"  frame {f.index:6d}  peak={f.histogram.max():7d}  "
                f"sum={f.histogram.sum():10d}  rate={count/elapsed:.2f}/s",
                end="\r",
            )
            last_print = time.time()
        if cfg.capture.interval_s > 0:
            time.sleep(cfg.capture.interval_s)
    return count


def _run_timed(cfg, sensor, writer, cb, stop, ctx) -> int:
    t_end = time.time() + cfg.capture.duration_s
    count = 0
    while not stop["stop"] and time.time() < t_end:
        _capture_and_dispatch(cfg, sensor, writer, cb, ctx)
        count += 1
        if cfg.capture.interval_s > 0:
            time.sleep(cfg.capture.interval_s)
    return count


def _run_manual(cfg, sensor, writer, cb, stop, ctx) -> int:
    """Capture on demand.

    Workflow:
      1. Capture one burst of ``num_frames`` frames at startup so the
         dashboard immediately shows something.
      2. Pause the device.
      3. Wait for a trigger (Enter on stdin, or the dashboard's Capture
         button via ``{"type": "capture"}`` over the WebSocket).
      4. Resume, capture one burst, pause again.
      5. Repeat 3-4 until Ctrl-C.

    Between bursts the device is idle (no measurement, no serial traffic),
    so the viz keeps the most recent burst visible without any new frames
    being captured.
    """
    burst = max(1, cfg.capture.num_frames)
    triggers: _queue.Queue = ctx.get("trigger_queue") or _queue.Queue()

    # Stdin reader thread. Each newline on stdin enqueues one trigger.
    def _stdin_loop() -> None:
        if not sys.stdin or not sys.stdin.readable():
            return
        while not stop["stop"]:
            try:
                line = sys.stdin.readline()
            except Exception:
                return
            if line == "":
                return  # EOF (e.g. nohup with /dev/null stdin)
            try:
                triggers.put_nowait(time.time())
            except Exception:
                pass

    threading.Thread(target=_stdin_loop, daemon=True).start()

    # The sensor exits __init__ with measurement running; pause it so the
    # device stays idle until each burst is explicitly triggered.
    sensor.pause_measurement()

    _console.print(
        f"[dim](manual mode · capturing one initial {burst}-frame burst, "
        f"then waiting for Enter or the dashboard's Capture button · Ctrl-C to stop)[/dim]"
    )

    saved = 0
    fire_burst = True   # initial burst on startup
    while not stop["stop"]:
        if fire_burst:
            sensor.resume_measurement()
            for _ in range(burst):
                if stop["stop"]:
                    break
                _capture_and_dispatch(cfg, sensor, writer, cb, ctx, write=True)
                saved += 1
            sensor.pause_measurement()
            _console.print(f"  saved {burst} frames · total {saved} · waiting for next trigger")
            fire_burst = False

        # Block (with periodic stop check) until a trigger arrives.
        try:
            triggers.get(timeout=0.5)
            fire_burst = True
        except _queue.Empty:
            continue

    return saved
