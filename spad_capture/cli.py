"""CLI: ``spad flash`` / ``spad capture`` / ``spad viz``."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import click

from spad_capture.capture import run_capture
from spad_capture.config import Config, ZoneMode, load_config
from spad_capture.errors import clean as _clean
from spad_capture.sensors.tmf.flash import flash as do_flash
from spad_capture.sensors.st.cli import st as _st_group
from spad_capture.viz.server import run_with_viz  # noqa: F401  (re-exported for downstream)

_ZONE_CHOICES = [m.value for m in ZoneMode]


# Maps a CLI keyword to the config path it overrides. Single source of truth
# for the flag <-> config-field correspondence documented in
# docs/options.md § CLI. A flag left unset (None) leaves the config
# untouched, so boolean pairs like --viz/--no-viz override in both
# directions.
_OVERRIDES: dict[str, tuple[str, ...]] = {
    "name":              ("name",),
    "port":              ("sensor", "port"),
    "zone":              ("sensor", "zone_mode"),
    "range_":            ("sensor", "range_mode"),
    "calibrate":         ("sensor", "calibrate"),
    "kilo_iter":         ("firmware", "kilo_iterations"),
    "period_ms":         ("firmware", "period_ms"),
    "mode":              ("capture", "mode"),
    "num_frames":        ("capture", "num_frames"),
    "duration":          ("capture", "duration_s"),
    "interval":          ("capture", "interval_s"),
    "samples_per_frame": ("capture", "samples_per_frame"),
    "fmt":               ("storage", "format"),
    "output_dir":        ("storage", "root"),
    "viz":               ("viz", "enabled"),
    "viz_port":          ("viz", "port"),
    "rgb":               ("rgb", "enabled"),
    "save_depth":        ("rgb", "save_depth"),
    "ir_left":           ("rgb", "ir_left"),
    "ir_right":          ("rgb", "ir_right"),
    "ir_no_dots":        ("rgb", "ir_no_dots"),
}


def _override(cfg: Config, **kw) -> Config:
    """Apply CLI overrides to a loaded config.

    Each entry of ``_OVERRIDES`` maps a CLI keyword to its config path.
    ``None`` means the flag was not supplied and the config value stands;
    every other value (including ``False`` and ``0``) is applied.
    """
    data = cfg.model_dump()
    for key, path in _OVERRIDES.items():
        value = kw.get(key)
        if value is None:
            continue
        target = data
        for part in path[:-1]:
            target = target[part]
        target[path[-1]] = value
    return Config(**data)


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
def main() -> None:
    """spad_capture command-line interface."""


@main.group("tmf")
def tmf() -> None:
    """AMS OSRAM TMF8828."""


@tmf.group("mask")
def mask_group() -> None:
    """Inspect and validate custom SPAD-mask configurations."""


@mask_group.command("validate")
@click.argument("path", type=click.Path(path_type=Path, exists=True))
def mask_validate_cmd(path: Path) -> None:
    """Check a mask config against the device's documented rules."""
    import yaml
    from spad_capture.sensors.tmf.mask import CustomMask, validate, preview
    raw = yaml.safe_load(path.read_text()) or {}
    section = raw.get("mask", raw)  # accept either a full config or a bare mask dict
    if not isinstance(section, dict):
        raise click.ClickException(f"No `mask:` section found in {path}.")
    mask = CustomMask(**section)
    v = validate(mask)
    click.echo(preview(mask))
    click.echo("")
    n_user = sum(1 for rz in v.resolved if not rz.is_dummy)
    n_dummy = sum(1 for rz in v.resolved if rz.is_dummy)
    click.echo(f"bbox  = rows {v.bbox[0]}..{v.bbox[2]}, cols {v.bbox[1]}..{v.bbox[3]}")
    click.echo(f"size  = {v.x_size} x {v.y_size}")
    click.echo(f"zones = {n_user} user" + (f"  +  {n_dummy} auto-dummy" if n_dummy else ""))
    if v.ok:
        click.echo(click.style("\nVALID", fg="green", bold=True))
    else:
        click.echo(click.style("\nINVALID:", fg="red", bold=True))
        for e in v.errors:
            click.echo(f"  • {e}")
        raise click.exceptions.Exit(1)


@mask_group.command("preview")
@click.argument("path", type=click.Path(path_type=Path, exists=True))
@click.option("--bbox-only", is_flag=True, help="Show only the bounding box, not the full 12x18 area.")
def mask_preview_cmd(path: Path, bbox_only: bool) -> None:
    """Render an ASCII visualization of a mask config."""
    import yaml
    from spad_capture.sensors.tmf.mask import CustomMask, preview
    raw = yaml.safe_load(path.read_text()) or {}
    section = raw.get("mask", raw)
    mask = CustomMask(**section)
    click.echo(preview(mask, full_area=not bbox_only))


@tmf.command("flash")
@click.option("--port", default=None, help="Serial port. Auto-detect if omitted.")
@click.option("--arduino-cli", "arduino_cli", type=click.Path(path_type=Path), default=None,
              help="Path to the arduino-cli binary. Auto-discovered if omitted.")
@click.option("-v", "--verbose", is_flag=True, help="Verbose arduino-cli output.")
def flash_cmd(port: Optional[str], arduino_cli: Optional[Path], verbose: bool) -> None:
    """Compile and upload the bundled TMF8828 sketch to the Arduino."""
    do_flash(port=port, arduino_cli=arduino_cli, verbose=verbose)


@tmf.command("capture")
@click.option("-c", "--config", "config_path", type=click.Path(path_type=Path), default=None,
              help="YAML config file (defaults if omitted).")
@click.option("-m", "--mask", "mask_path", type=click.Path(path_type=Path, exists=True), default=None,
              help="Custom mask YAML (sets zone_mode=custom and overrides mask in config).")
@click.option("--port", default=None, help="Serial port override.")
@click.option("--zone", type=click.Choice(_ZONE_CHOICES), default=None,
              help="Zone-mode override (see docs/options.md).")
@click.option("--kilo-iter", "kilo_iter", type=int, default=None,
              help="VCSEL pulses per frame, divided by 1024. Higher values "
                   "give more signal at a lower frame rate. Firmware-baked "
                   "defaults: 10000 (3x3 maps), 2500 (4x4 maps), 10 (8x8).")
@click.option("--period-ms", "period_ms", type=int, default=None,
              help="Override firmware measurement period (ms).")
@click.option("--range", "range_", type=click.Choice(["long", "short"]), default=None,
              help="Range-mode override.")
@click.option("--mode", type=click.Choice(["sequential", "timed", "manual"]), default=None,
              help="Capture-mode override.")
@click.option("-n", "--num-frames", type=int, default=None, help="Sequential frame count.")
@click.option("-d", "--duration", type=float, default=None, help="Timed-mode duration (s); default 60.")
@click.option("-i", "--interval", type=float, default=None, help="Inter-frame sleep (s).")
@click.option("--samples-per-frame", "samples_per_frame", type=int, default=None,
              help="Sensor captures averaged per output frame.")
@click.option("-o", "--output-dir", type=click.Path(path_type=Path), default=None,
              help="Output root (each run creates a subfolder inside it).")
@click.option("-f", "--format", "fmt", type=click.Choice(["pkl", "npy", "h5", "none"]),
              default=None, help="Storage format.")
@click.option("--name", "name", default=None,
              help="Name embedded in the output folder.")
@click.option("--viz/--no-viz", default=None, help="Enable the live web dashboard.")
@click.option("--viz-port", type=int, default=None, help="Viz port (default 8888).")
@click.option("--bind-all", is_flag=True, default=False,
              help="Bind viz to 0.0.0.0 so other hosts on the network can connect.")
@click.option("--rgb/--no-rgb", default=None,
              help="Enable colocated Realsense RGB capture.")
@click.option("--save-depth/--no-save-depth", "save_depth", default=None,
              help="When --rgb is on, also save depth frames.")
@click.option("--ir-left/--no-ir-left", "ir_left", default=None,
              help="Capture the left IR image (infrared 1).")
@click.option("--ir-right/--no-ir-right", "ir_right", default=None,
              help="Capture the right IR image (infrared 2).")
@click.option("--ir-no-dots/--ir-dots", "ir_no_dots", default=None,
              help="Keep the dot projector off so IR shows no projected pattern.")
@click.option("--calibrate/--no-calibrate", "calibrate", default=None,
              help="Run factory crosstalk calibration on the active mask "
                   "before capture starts. Needs a dark housing.")
def capture_cmd(config_path, mask_path, port, zone, range_, mode, num_frames, duration, interval,
                 samples_per_frame, output_dir, fmt, name, viz, viz_port, bind_all, rgb,
                 save_depth, ir_left, ir_right, ir_no_dots, calibrate, kilo_iter,
                 period_ms) -> None:
    """Capture frames according to the config (+ optional overrides)."""
    try:
        cfg = load_config(config_path)
        cfg = _override(
            cfg, port=port, zone=zone, range_=range_, mode=mode,
            num_frames=num_frames, duration=duration, interval=interval,
            samples_per_frame=samples_per_frame,
            output_dir=output_dir, fmt=fmt, name=name,
            viz=viz, viz_port=viz_port,
            kilo_iter=kilo_iter, period_ms=period_ms,
            rgb=rgb, save_depth=save_depth, ir_left=ir_left, ir_right=ir_right,
            ir_no_dots=ir_no_dots,
            calibrate=calibrate,
        )
        if mask_path is not None:
            import yaml as _yaml
            raw = _yaml.safe_load(mask_path.read_text()) or {}
            section = raw.get("mask", raw) if isinstance(raw, dict) else None
            if not isinstance(section, dict):
                raise click.ClickException(f"No `mask:` section found in {mask_path}.")
            data = cfg.model_dump()
            data["mask"] = section
            data["sensor"]["zone_mode"] = "custom"
            cfg = Config(**data)
        if bind_all:
            cfg.viz.host = "0.0.0.0"

        if cfg.viz.enabled:
            run_with_viz(cfg, run_capture)
        else:
            run_capture(cfg)
    except (ValueError, FileNotFoundError, RuntimeError) as e:
        raise _clean(e) from None


@tmf.command("viz")
@click.option("-c", "--config", "config_path", type=click.Path(path_type=Path), default=None,
              help="YAML config file (only viz section used).")
@click.option("--host", default=None, help="Bind host (default 127.0.0.1).")
@click.option("--port", type=int, default=None, help="Bind port (default 8888).")
@click.option("--bind-all", is_flag=True, default=False,
              help="Bind to 0.0.0.0 so other hosts on the network can connect.")
@click.option("--source", type=click.Path(path_type=Path), default=None,
              help="Replay a saved capture (.pkl / .npy / .h5) instead of live.")
@click.option("--rate-hz", type=float, default=5.0, help="Replay rate in frames/sec.")
def viz_cmd(config_path, host, port, bind_all, source, rate_hz) -> None:
    """Run the live viz server. With --source it replays a saved capture."""
    import time
    from spad_capture.frame import Frame
    from spad_capture.viz.server import VizServer
    from spad_capture.storage import load

    cfg = load_config(config_path)
    if host is not None:
        cfg.viz.host = host
    elif bind_all:
        cfg.viz.host = "0.0.0.0"
    if port is not None:
        cfg.viz.port = port

    from spad_capture.sensors.tmf.predefined_layouts import build_zone_meta as _mask_meta_for_full

    def _mask_meta_for(zone_mode: str, mask_dict, sensor_layout=None) -> dict:
        """Build the mask-related meta the viz uses to label outputs and
        render the info modal. Identical shape for predefined and custom modes."""
        return _mask_meta_for_full(zone_mode, mask_dict, sensor_layout=sensor_layout)

    server = VizServer(
        cfg.viz,
        sensor_meta={
            "zone_mode": cfg.sensor.zone_mode.value,
            "range_mode": cfg.sensor.range_mode.value,
            "rgb_enabled": cfg.rgb.enabled,
            **_mask_meta_for(cfg.sensor.zone_mode.value, cfg.mask),
        },
        jpeg_quality=cfg.rgb.jpeg_quality,
    )

    controller = None
    if source is not None:
        from spad_capture.viz.server import ReplayController
        src_meta, recs = load(source)
        if not recs:
            raise click.ClickException(f"No frames in {source}")
        frames = [
            Frame(
                index=r["index"], timestamp=r["timestamp"], histogram=r["histogram"],
                rgb_bgr=r.get("rgb_bgr"), depth_mm=r.get("depth_mm"),
                rgb_intrinsics=r.get("rgb_intrinsics"),
            )
            for r in recs
        ]
        if (s := src_meta.get("sensor")):
            server.sensor_meta["zone_mode"] = s.get("zone_mode", server.sensor_meta.get("zone_mode"))
            server.sensor_meta["range_mode"] = s.get("range_mode", server.sensor_meta.get("range_mode"))
        # Rebuild the mask meta from the saved sensor_layout (canonical),
        # falling back to zone_mode + mask dict. Drop the live-mode
        # mask_preview carry-over first.
        server.sensor_meta.pop("mask_preview", None)
        server.sensor_meta.update(_mask_meta_for(
            server.sensor_meta["zone_mode"], src_meta.get("mask"),
            sensor_layout=src_meta.get("sensor_layout")))
        server.sensor_meta["rgb_enabled"] = any(f.rgb_bgr is not None for f in frames)
        server.sensor_meta["depth_enabled"] = any(f.depth_mm is not None for f in frames)
        # Forward the full saved metadata so the playback info modal can
        # render calibration, sensor_layout, capture, and firmware tabs.
        server.sensor_meta["replay_meta"] = {
            "calibration": src_meta.get("calibration"),
            "sensor_layout": src_meta.get("sensor_layout"),
            "capture": src_meta.get("capture"),
            "firmware": src_meta.get("firmware"),
            "rgb": src_meta.get("rgb"),
            "mask": src_meta.get("mask"),
            "created_at": src_meta.get("created_at"),
            "version": src_meta.get("version"),
            "source": str(source),
            "frames": len(frames),
        }
        controller = ReplayController(server, frames, rate_hz=rate_hz)
        server.replay = controller

    server.start()
    from spad_capture.viz.server import viz_urls as _viz_urls
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    c = Console()
    tbl = Table.grid(padding=(0, 2))
    tbl.add_column(style="dim", width=6)
    tbl.add_column()
    if controller is not None:
        controller.start()
        tbl.add_row("mode", f"replay  ·  {len(controller.frames)} frames @ {rate_hz} Hz")
    else:
        tbl.add_row("mode", "live (waiting for publisher)")
    if source is not None:
        tbl.add_row("source", str(source))
    tbl.add_row("open", "\n".join(f"[link={u}]{u}[/link]" for u in _viz_urls(cfg.viz)))
    c.print(Panel(tbl, title="[bold]spad viz[/bold]", title_align="left",
                  border_style="cyan", padding=(0, 1)))
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        if controller is not None:
            controller.stop()
        server.stop()


@main.group("camera")
def camera_group() -> None:
    """Colocated Realsense."""


@camera_group.command("check")
def camera_check_cmd() -> None:
    """Report whether the Realsense is usable, and what to do if not."""
    from rich.console import Console

    from spad_capture.macos import camera_state, fix_for
    c = Console()
    state, detail = camera_state()
    if state == "ok":
        c.print(f"[bold green]ok[/bold green]  {detail}")
        return
    c.print(f"[bold red]{state}[/bold red]  {detail}\n"
            f"[yellow]fix[/yellow]  {fix_for(state)}")
    raise SystemExit(1)


main.add_command(_st_group)
