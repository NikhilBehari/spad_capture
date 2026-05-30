"""Compile and upload the bundled TMF8828 sketch via arduino-cli."""

from __future__ import annotations

import os
import shutil
import subprocess
import urllib.request
from pathlib import Path
from typing import Optional

from rich.console import Console

from spad_capture.firmware import firmware_dir, sketch_path
from spad_capture.sensor import find_arduino_port

_console = Console()

# Fallback search paths for arduino-cli when not on PATH.
_DEFAULT_CLI_CANDIDATES = [
    Path("/usr/local/bin/arduino-cli"),
    Path("~/.local/bin/arduino-cli").expanduser(),
]

_FQBN = "arduino:avr:uno"
_BOOTSTRAP_URL = "https://raw.githubusercontent.com/arduino/arduino-cli/master/install.sh"


def _find_arduino_cli(override: Optional[Path] = None) -> Optional[Path]:
    """Locate the arduino-cli binary, or return None if missing."""
    if override:
        if not override.exists():
            raise FileNotFoundError(f"--arduino-cli path does not exist: {override}")
        return override
    on_path = shutil.which("arduino-cli")
    if on_path:
        return Path(on_path)
    for cand in _DEFAULT_CLI_CANDIDATES:
        if cand.exists():
            return cand
    return None


def _bootstrap_dir() -> Path:
    """Where to drop arduino-cli when bootstrapping. Prefers the active
    conda env's bin so the binary is scoped to the env."""
    conda_prefix = os.environ.get("CONDA_PREFIX")
    if conda_prefix:
        return Path(conda_prefix) / "bin"
    return Path("~/.local/bin").expanduser()


def _arduino_cli_version(binary: Path) -> Optional[str]:
    """Return the first line of ``arduino-cli version`` output, or None if
    the binary cannot be executed."""
    try:
        p = subprocess.run(
            [str(binary), "version"], capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode != 0:
        return None
    return p.stdout.strip().splitlines()[0] if p.stdout.strip() else ""


def _bootstrap_arduino_cli() -> Path:
    """Install arduino-cli into the active environment.

    Skips the download if a usable binary already exists at the bootstrap
    target. Otherwise downloads the upstream install script and runs it
    with ``BINDIR`` set to the conda env's ``bin`` (or
    ``~/.local/bin``).

    Supported on Linux and macOS. Windows users must install arduino-cli
    manually and pass ``--arduino-cli <path>``.
    """
    if shutil.which("sh") is None:
        raise RuntimeError(
            "arduino-cli auto-install requires a POSIX shell (sh). "
            "Install arduino-cli manually from "
            "https://arduino.github.io/arduino-cli/latest/installation/ "
            "and pass --arduino-cli <path>."
        )

    target = _bootstrap_dir()
    binary = target / "arduino-cli"

    # Idempotency: reuse an existing binary if it runs.
    if binary.exists():
        version = _arduino_cli_version(binary)
        if version:
            _console.print(f"[dim]arduino-cli already installed: {version}[/dim]")
            return binary
        _console.print(
            f"[yellow]arduino-cli at {binary} did not run; reinstalling.[/yellow]"
        )

    target.mkdir(parents=True, exist_ok=True)
    _console.print(f"Installing arduino-cli into [cyan]{target}[/cyan] ...")
    try:
        with urllib.request.urlopen(_BOOTSTRAP_URL, timeout=30) as r:
            script = r.read()
    except Exception as e:
        raise RuntimeError(
            f"Could not download arduino-cli installer from {_BOOTSTRAP_URL}: {e}.\n"
            "Install arduino-cli manually and pass --arduino-cli <path>."
        )

    # The install script reads the install directory from BINDIR.
    env = os.environ.copy()
    env["BINDIR"] = str(target)
    proc = subprocess.run(
        ["sh"], input=script, env=env, capture_output=True, check=False,
    )
    if proc.returncode != 0 or not binary.exists():
        detail = (
            proc.stdout.decode("utf-8", errors="replace")
            + proc.stderr.decode("utf-8", errors="replace")
        ).strip()
        raise RuntimeError(
            "arduino-cli installer failed."
            + (f"\n{detail}" if detail else "")
            + "\nInstall arduino-cli manually and pass --arduino-cli <path>."
        )
    version = _arduino_cli_version(binary)
    if version is None:
        raise RuntimeError(f"arduino-cli installed at {binary} but does not run.")
    _console.print(f"[green]Installed[/green] {version}")
    return binary


def _ensure_avr_core(cli: Path) -> None:
    """Install the arduino:avr core if it is not already registered."""
    out = subprocess.run([str(cli), "core", "list"], capture_output=True, text=True)
    if "arduino:avr" in out.stdout:
        return
    _console.print("Installing arduino:avr core ...")
    subprocess.run([str(cli), "core", "update-index"], check=True, capture_output=True)
    subprocess.run([str(cli), "core", "install", "arduino:avr"], check=True, capture_output=True)
    _console.print("[green]arduino:avr core installed[/green]")


def flash(
    port: Optional[str] = None,
    *,
    arduino_cli: Optional[Path] = None,
    verbose: bool = False,
) -> None:
    """Compile and upload the bundled TMF8828 sketch to the Arduino."""
    port = port or find_arduino_port()
    cli = _find_arduino_cli(arduino_cli)
    if cli is None:
        cli = _bootstrap_arduino_cli()
    sketch = sketch_path()
    if not sketch.exists():
        raise FileNotFoundError(f"Bundled firmware missing: {sketch}")

    _ensure_avr_core(cli)

    _console.print(
        f"[bold]Flashing[/bold] [cyan]{sketch.name}[/cyan] "
        f"on [cyan]{port}[/cyan] ({_FQBN})"
    )

    cmd = [str(cli), "compile", "--upload", "--port", port, "--fqbn", _FQBN, str(sketch)]
    if verbose:
        cmd.insert(1, "-v")

    env = os.environ.copy()
    proc = subprocess.run(cmd, env=env)
    if proc.returncode != 0:
        raise RuntimeError(f"arduino-cli flash failed with exit code {proc.returncode}")

    _console.print("[bold green]Flash complete.[/bold green]")
