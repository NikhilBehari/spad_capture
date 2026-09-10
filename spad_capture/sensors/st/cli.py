"""CLI: ``spadst detect`` / ``spadst info`` / ``spadst capture`` / ``spadst viz``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import click
from pydantic import ValidationError

from spad_capture.errors import clean as _clean
from spad_capture.sensors.st.config import Config, Mode, load_config

_MODE_CHOICES = [m.value for m in Mode]


def _override(cfg: Config, **kw) -> Config:
    """Apply scalar CLI overrides to a loaded config, then re-validate fully."""
    data = cfg.model_dump()
    if kw.get("port") is not None:
        data["sensor"]["port"] = kw["port"]
    if kw.get("mode") is not None:
        data["sensor"]["mode"] = kw["mode"]
    if kw.get("start_mm") is not None:
        data["sensor"]["start_mm"] = kw["start_mm"]
    if kw.get("end_mm") is not None:
        data["sensor"]["end_mm"] = kw["end_mm"]
    if kw.get("bin_mm") is not None:
        data["sensor"]["bin_mm"] = kw["bin_mm"]
    if kw.get("freq") is not None:
        data["sensor"]["ranging_frequency_hz"] = kw["freq"]
    if kw.get("integ_ms") is not None:
        data["sensor"]["integration_time_ms"] = kw["integ_ms"]
    if kw.get("ranging_mode") is not None:
        data["sensor"]["ranging_mode"] = kw["ranging_mode"]
    if kw.get("zone") is not None:
        z = kw["zone"]
        if isinstance(z, str):
            parts = z.split(",")
            if len(parts) != 2:
                raise ValueError(f"--zone must be 'ROW,COL' (got {z!r}).")
            try:
                z = (int(parts[0]), int(parts[1]))
            except ValueError:
                raise ValueError(f"--zone must be integers 'ROW,COL' (got {z!r}).")
        data["sensor"]["zone"] = list(z)
    if kw.get("roi") is not None:
        r = kw["roi"]
        if isinstance(r, str):
            try:
                r = json.loads(r)
            except json.JSONDecodeError:
                parts = r.split(",")
                if len(parts) != 4:
                    raise ValueError(
                        f"--roi must be JSON or 'START_ROW,START_COL,ROWS,COLS' (got {r!r}).")
                try:
                    sr, sc, rows, cols = (int(p) for p in parts)
                except ValueError:
                    raise ValueError(f"--roi values must be integers (got {r!r}).")
                r = {"start_row": sr, "start_col": sc, "rows": rows, "cols": cols}
        data["sensor"]["roi"] = r
    if kw.get("cap_mode") is not None:
        data["capture"]["mode"] = kw["cap_mode"]
    if kw.get("num_frames") is not None:
        data["capture"]["num_frames"] = kw["num_frames"]
    if kw.get("sum_frames") is not None:
        data["capture"]["sum_frames"] = kw["sum_frames"]
    if kw.get("bg_subtract"):
        data["capture"]["bg_subtract"] = True
    if kw.get("duration") is not None:
        data["capture"]["duration_s"] = kw["duration"]
    if kw.get("interval") is not None:
        data["capture"]["interval_s"] = kw["interval"]
    if kw.get("output_dir") is not None:
        data["storage"]["root"] = kw["output_dir"]
    if kw.get("fmt") is not None:
        data["storage"]["format"] = kw["fmt"]
    if kw.get("name") is not None:
        data["name"] = kw["name"]
    if kw.get("viz") is True:
        data["viz"]["enabled"] = True
    elif kw.get("viz") is False:
        data["viz"]["enabled"] = False
    if kw.get("viz_port") is not None:
        data["viz"]["port"] = kw["viz_port"]
    # -- Realsense (RGB / depth / IR) ---------------------------------------
    if kw.get("rgb") is True:
        data["rgb"]["enabled"] = True
    elif kw.get("rgb") is False:
        data["rgb"]["enabled"] = False
    if kw.get("save_depth") is True:
        data["rgb"]["save_depth"] = True
    if kw.get("depth_native") is True:
        data["rgb"]["align_depth"] = False
    if kw.get("ir_left") is not None:
        data["rgb"]["ir_left"] = kw["ir_left"]
    if kw.get("ir_right") is not None:
        data["rgb"]["ir_right"] = kw["ir_right"]
    if kw.get("rs_width") is not None:
        data["rgb"]["width"] = kw["rs_width"]
    if kw.get("rs_height") is not None:
        data["rgb"]["height"] = kw["rs_height"]
    if kw.get("rs_fps") is not None:
        data["rgb"]["fps"] = kw["rs_fps"]
    return Config(**data)


@click.group("st")
def st() -> None:
    """ST VL53L8CH."""


@st.command("flash")
@click.option("--port", default=None, help="Serial port. Auto-detect if omitted.")
@click.option("--arduino-cli", "arduino_cli", type=click.Path(path_type=Path), default=None,
              help="Path to arduino-cli. Auto-installed if missing.")
@click.option("-v", "--verbose", is_flag=True, help="Verbose arduino-cli output.")
def flash_cmd(port: Optional[str], arduino_cli: Optional[Path], verbose: bool) -> None:
    """Compile and upload the bundled VL53L8CH sketch to the NUCLEO board."""
    from spad_capture.sensors.st.flash import flash as do_flash
    try:
        do_flash(port, arduino_cli=arduino_cli, verbose=verbose)
    except Exception as e:
        raise _clean(e)


@st.command("detect")
@click.option("--port", default=None, help="Serial port override (auto-detect if omitted).")
def detect_cmd(port: Optional[str]) -> None:
    """Run the detection gate + handshake; print the device id or a precise abort."""
    from spad_capture.sensors.st.config import SensorConfig
    from spad_capture.sensors.st.transport import (HandshakeError, PortNotFoundError,
                                          VL53L8CHTransport)

    cfg = Config(sensor=SensorConfig(port=port))
    try:
        with VL53L8CHTransport(cfg) as t:
            info = t.handshake()
    except (PortNotFoundError, HandshakeError) as e:
        raise click.ClickException(str(e)) from None
    dev_id = info.get("device_id")
    ver = (
        f"{info.get('fw_major', '?')}.{info.get('fw_minor', '?')}"
        if "fw_major" in info else "?"
    )
    id_str = f"0x{dev_id:02X}" if dev_id is not None else "?"
    click.echo(click.style("VL53L8CH detected", fg="green", bold=True))
    click.echo(f"  port      = {cfg.sensor.port or 'auto'}")
    click.echo(f"  device id = {id_str}")
    click.echo(f"  firmware  = {ver}")


@st.command("info")
@click.option("-c", "--config", "config_path", type=click.Path(path_type=Path), default=None,
              help="YAML config file (defaults if omitted).")
@click.option("--mode", type=click.Choice(_MODE_CHOICES), default=None, help="Zone-grid override.")
@click.option("--start-mm", "start_mm", type=float, default=None, help="Window start (mm).")
@click.option("--end-mm", "end_mm", type=float, default=None, help="Window end (mm).")
@click.option("--bin-mm", "bin_mm", type=float, default=None, help="Requested bin width (mm).")
@click.option("--zone", "zone", default=None,
              help="Single output zone ROW,COL (e.g. --zone 3,4; valid 0..3 for 4x4, "
                   "0..7 for 8x8). Unlocks up to 255 bins per zone, overriding the "
                   "full-grid budget caps. Overrides --roi; omit for the full grid.")
@click.option("--roi", "roi", default=None,
              help="Rectangular ROI as JSON {'start_row','start_col','rows','cols'} or "
                   "'START_ROW,START_COL,ROWS,COLS' (YAML config preferred).")
@click.option("--freq", type=int, default=None, help="Ranging frequency (Hz, 1..30).")
@click.option("--integ-ms", "integ_ms", type=int, default=None,
              help="Integration time (ms): exposure lever in autonomous mode only "
                   "(device range 2-1000 ms); in continuous mode the device "
                   "auto-integrates to the ranging period and this value is ignored.")
@click.option("--ranging-mode", "ranging_mode",
              type=click.Choice(["continuous", "autonomous"]), default=None,
              help="Ranging-mode override.")
def info_cmd(config_path, mode, start_mm, end_mm, bin_mm, zone, roi,
             freq, integ_ms, ranging_mode) -> None:
    """Print the resolved capture geometry (bins, window, budget) WITHOUT opening the port."""
    from rich.console import Console
    from rich.table import Table

    try:
        cfg = load_config(config_path)
        cfg = _override(cfg, mode=mode, start_mm=start_mm, end_mm=end_mm, bin_mm=bin_mm,
                        zone=zone, roi=roi, freq=freq, integ_ms=integ_ms,
                        ranging_mode=ranging_mode)
    except (FileNotFoundError, ValueError) as e:
        raise _clean(e) from None

    r = cfg.resolved()
    tbl = Table.grid(padding=(0, 2))
    tbl.add_column(style="dim", width=16)
    tbl.add_column()
    tbl.add_row("mode", f"{r['mode']}  ({r['height']}x{r['width']}, {r['zones']} zones)")
    if r["output_zones"] != r["zones"]:
        tbl.add_row("output",
                    f"{r['output_height']}x{r['output_width']}  "
                    f"({r['output_zones']} zone(s) @ start row={r['agg_start_y']}, "
                    f"col={r['agg_start_x']})")
    else:
        tbl.add_row("output", "full grid")
    tbl.add_row("window", f"{r['start_mm']:.1f} – {r['end_mm']:.1f} mm")
    tbl.add_row("num_bins", str(r["num_bins"]))
    tbl.add_row("start_bin", str(r["start_bin"]))
    tbl.add_row("binning_factor", str(r["binning_factor"]))
    tbl.add_row("effective_bin", f"{r['effective_bin_mm']:.2f} mm  (native {r['native_bin_mm']:.2f} mm)")
    tbl.add_row("frequency", f"{r['ranging_frequency_hz']} Hz  ·  {r['ranging_mode']}")
    tbl.add_row("integration", f"{r['integration_time_ms']} ms")
    tbl.add_row("budget", f"{r['budget_bytes']} / {r['budget_limit']} bytes")
    Console().print(tbl)


_COMMON_OVERRIDES = [
    click.option("-c", "--config", "config_path", type=click.Path(path_type=Path), default=None,
                 help="YAML config file (defaults if omitted)."),
    click.option("--port", default=None, help="Serial port override."),
    click.option("--mode", type=click.Choice(_MODE_CHOICES), default=None, help="Zone-grid override."),
    click.option("--start-mm", "start_mm", type=float, default=None, help="Window start (mm)."),
    click.option("--end-mm", "end_mm", type=float, default=None, help="Window end (mm)."),
    click.option("--bin-mm", "bin_mm", type=float, default=None, help="Requested bin width (mm)."),
    click.option("--zone", "zone", default=None,
                 help="Single output zone ROW,COL (e.g. --zone 3,4; valid 0..3 for 4x4, "
                      "0..7 for 8x8). Unlocks up to 255 bins per zone, overriding the "
                      "full-grid budget caps. Overrides --roi; omit for the full grid."),
    click.option("--roi", "roi", default=None,
                 help="Rectangular ROI as JSON {'start_row','start_col','rows','cols'} or "
                      "'START_ROW,START_COL,ROWS,COLS' (YAML config preferred)."),
    click.option("--freq", type=int, default=None, help="Ranging frequency (Hz, 1..30)."),
    click.option("--integ-ms", "integ_ms", type=int, default=None,
                 help="Integration time (ms): exposure lever in autonomous mode only "
                      "(device range 2-1000 ms); in continuous mode the device "
                      "auto-integrates to the ranging period and this value is ignored."),
    click.option("--ranging-mode", "ranging_mode",
                 type=click.Choice(["continuous", "autonomous"]), default=None,
                 help="Ranging-mode override."),
    click.option("--cap-mode", "cap_mode", type=click.Choice(["stream", "manual"]), default=None,
                 help="Capture-mode override."),
    click.option("-n", "--num-frames", "num_frames", type=int, default=None,
                 help="Stream count / manual burst size."),
    click.option("-d", "--duration", type=float, default=None, help="Stream duration (s)."),
    click.option("-i", "--interval", type=float, default=None, help="Inter-frame sleep (s)."),
    click.option("-o", "--output-dir", "output_dir", type=click.Path(path_type=Path), default=None,
                 help="Output root (each run creates a subfolder inside it)."),
    click.option("-f", "--format", "fmt", type=click.Choice(["npz", "none"]), default=None,
                 help="Storage format (npz = save data.npz, none = capture without saving)."),
    click.option("--name", default=None, help="Name embedded in the output folder."),
]


def _with_common_overrides(fn):
    for opt in reversed(_COMMON_OVERRIDES):
        fn = opt(fn)
    return fn


@st.command("capture")
@_with_common_overrides
@click.option("--viz/--no-viz", default=None, help="Enable the live web dashboard.")
@click.option("--viz-port", "viz_port", type=int, default=None, help="Viz port (default 8888).")
@click.option("--bind-all", "bind_all", is_flag=True, default=False,
              help="Bind viz to 0.0.0.0 so other hosts can connect.")
@click.option("--rgb/--no-rgb", default=None,
              help="Stream the colocated Realsense RGB camera (background thread).")
@click.option("--save-depth", "save_depth", is_flag=True, default=False,
              help="Also capture Realsense depth (manual: one dense post-burst shot; "
                   "stream: latest buffered depth per frame).")
@click.option("--depth-native", "depth_native", is_flag=True, default=False,
              help="Keep depth in its native left-IR frame instead of aligning to color "
                   "(only matters when --rgb is also set; depth-only capture stays native).")
@click.option("--ir-left/--no-ir-left", "ir_left", default=None,
              help="Capture the left Realsense IR image (infrared 1).")
@click.option("--ir-right/--no-ir-right", "ir_right", default=None,
              help="Capture the right Realsense IR image (infrared 2).")
@click.option("--rs-width", "rs_width", type=int, default=None,
              help="Realsense stream width (default 640; 848 needs USB-3).")
@click.option("--rs-height", "rs_height", type=int, default=None,
              help="Realsense stream height (default 480).")
@click.option("--rs-fps", "rs_fps", type=int, default=None, help="Realsense fps (default 30).")
@click.option("--sum/--no-sum", "sum_frames", default=None,
              help="Manual mode: show the SUM of each burst's frames in the GUI "
                   "(raw frames are still saved individually).")
@click.option("--bg-subtract", "bg_subtract", is_flag=True, default=False,
              help="Measure the empty-scene background at startup (point at nothing) "
                   "and subtract it from the displayed histogram; raw stays un-subtracted.")
def capture_cmd(config_path, port, mode, start_mm, end_mm, bin_mm, zone, roi,
                freq, integ_ms, ranging_mode,
                cap_mode, num_frames, duration, interval, output_dir, fmt, name,
                viz, viz_port, bind_all, rgb, save_depth, depth_native, ir_left, ir_right,
                rs_width, rs_height, rs_fps, sum_frames, bg_subtract) -> None:
    """Capture frames. Gates on ST-LINK detection + handshake first."""
    from spad_capture.sensors.st.controller import run_capture, run_with_viz
    from spad_capture.sensors.st.parser import FrameError
    from spad_capture.sensors.st.transport import HandshakeError, PortNotFoundError

    try:
        cfg = load_config(config_path)
        cfg = _override(
            cfg, port=port, mode=mode, start_mm=start_mm, end_mm=end_mm, bin_mm=bin_mm,
            zone=zone, roi=roi,
            freq=freq, integ_ms=integ_ms, ranging_mode=ranging_mode, cap_mode=cap_mode,
            num_frames=num_frames, duration=duration, interval=interval,
            output_dir=output_dir, fmt=fmt, name=name, viz=viz, viz_port=viz_port,
            rgb=rgb, save_depth=save_depth, depth_native=depth_native,
            ir_left=ir_left, ir_right=ir_right,
            rs_width=rs_width, rs_height=rs_height, rs_fps=rs_fps, sum_frames=sum_frames,
            bg_subtract=bg_subtract,
        )
        if bind_all:
            cfg.viz.host = "0.0.0.0"
        if cfg.viz.enabled:
            run_with_viz(cfg, run_capture)
        else:
            run_capture(cfg)
    except (PortNotFoundError, HandshakeError, FrameError, ValueError,
            FileNotFoundError, RuntimeError) as e:
        raise _clean(e) from None


@st.command("viz")
@click.option("-c", "--config", "config_path", type=click.Path(path_type=Path), default=None,
              help="YAML config file (defaults if omitted).")
@click.option("--host", default=None, help="Bind host (default 127.0.0.1).")
@click.option("--port", "viz_port", type=int, default=None, help="Bind port (default 8888).")
@click.option("--bind-all", "bind_all", is_flag=True, default=False,
              help="Bind to 0.0.0.0 so other hosts can connect.")
@click.option("--source", type=click.Path(path_type=Path), default=None,
              help="Replay a saved capture (.npz) instead of a live one.")
@click.option("--rate-hz", "rate_hz", type=float, default=5.0, help="Replay rate in frames/sec.")
def viz_cmd(config_path, host, viz_port, bind_all, source, rate_hz) -> None:
    """Serve the live dashboard, or replay a saved capture with --source."""
    from spad_capture.sensors.st.controller import run_capture, run_with_viz
    from spad_capture.sensors.st.parser import FrameError
    from spad_capture.sensors.st.transport import HandshakeError, PortNotFoundError

    try:
        cfg = load_config(config_path)
        cfg.viz.enabled = True
        if host is not None:
            cfg.viz.host = host
        elif bind_all:
            cfg.viz.host = "0.0.0.0"
        if viz_port is not None:
            cfg.viz.port = viz_port
        if source is not None:
            _replay(cfg, source, rate_hz)
            return
        run_with_viz(cfg, run_capture)
    except (PortNotFoundError, HandshakeError, FrameError, ValueError,
            FileNotFoundError, RuntimeError) as e:
        raise _clean(e) from None


def _replay(cfg, source: Path, rate_hz: float) -> None:
    """Drive the dashboard from a saved run. No device is opened."""
    import time
    from spad_capture.frame import Frame
    from spad_capture.storage import load
    from spad_capture.viz.server import ReplayController, VizServer, viz_urls

    meta, recs = load(source)
    if not recs:
        raise click.ClickException(f"No frames in {source}")
    frames = [Frame(index=r["index"], timestamp=r["timestamp"], histogram=r["histogram"],
                    ambient=r.get("ambient"), rgb_bgr=r.get("rgb_bgr"),
                    depth_mm=r.get("depth_mm"), ir_left=r.get("ir_left"),
                    ir_right=r.get("ir_right")) for r in recs]
    # sensor_layout is the resolved geometry the run was captured with.
    sensor_meta = dict(meta.get("sensor_layout") or {})
    sensor_meta["capture_mode"] = (meta.get("capture") or {}).get("mode", "")
    server = VizServer(cfg.viz, sensor_meta=sensor_meta,
                       jpeg_quality=cfg.rgb.jpeg_quality)
    controller = ReplayController(server, frames, rate_hz=rate_hz)
    server.replay = controller
    server.start()
    controller.start()
    click.echo(f"replay  {len(frames)} frames @ {rate_hz} Hz  ->  "
               + "  ".join(viz_urls(cfg.viz)))
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        controller.stop()
        server.stop()
