"""Shared arduino-cli plumbing: locate or install the CLI, install a core, upload a sketch.

Both backends flash through here; each supplies only its core, FQBN and sketch.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import urllib.request
from pathlib import Path
from typing import Optional

from rich.console import Console

_console = Console()

# Fallback search paths for arduino-cli when not on PATH.
_CLI_CANDIDATES = [
    Path("/usr/local/bin/arduino-cli"),
    Path("/opt/homebrew/bin/arduino-cli"),   # Homebrew on Apple silicon
    Path("~/.local/bin/arduino-cli").expanduser(),
]
_BOOTSTRAP_URL = "https://raw.githubusercontent.com/arduino/arduino-cli/master/install.sh"


def _bootstrap_dir() -> Path:
    """Where to drop arduino-cli. Prefers the active conda env's bin so the
    binary is scoped to the env."""
    prefix = os.environ.get("CONDA_PREFIX")
    return Path(prefix) / "bin" if prefix else Path("~/.local/bin").expanduser()


def _version(binary: Path) -> Optional[str]:
    """First line of ``arduino-cli version``, or None if it cannot be executed."""
    try:
        p = subprocess.run([str(binary), "version"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode != 0:
        return None
    return p.stdout.strip().splitlines()[0] if p.stdout.strip() else ""


def find_cli(override: Optional[Path] = None) -> Optional[Path]:
    """Locate the arduino-cli binary, or None if missing."""
    if override:
        if not override.exists():
            raise FileNotFoundError(f"--arduino-cli path does not exist: {override}")
        return override
    on_path = shutil.which("arduino-cli")
    if on_path:
        return Path(on_path)
    return next((c for c in _CLI_CANDIDATES if c.exists()), None)


def bootstrap_cli() -> Path:
    """Install arduino-cli into the active environment.

    Reuses an existing binary if it runs. Linux and macOS only; on Windows
    install arduino-cli manually and pass ``--arduino-cli <path>``.
    """
    if shutil.which("sh") is None:
        raise RuntimeError(
            "arduino-cli auto-install requires a POSIX shell (sh). Install it from "
            "https://arduino.github.io/arduino-cli/latest/installation/ "
            "and pass --arduino-cli <path>."
        )
    target = _bootstrap_dir()
    binary = target / "arduino-cli"
    if binary.exists():
        v = _version(binary)
        if v:
            _console.print(f"[dim]arduino-cli already installed: {v}[/dim]")
            return binary
        _console.print(f"[yellow]arduino-cli at {binary} did not run; reinstalling.[/yellow]")

    target.mkdir(parents=True, exist_ok=True)
    _console.print(f"Installing arduino-cli into [cyan]{target}[/cyan] ...")
    try:
        with urllib.request.urlopen(_BOOTSTRAP_URL, timeout=30) as r:
            script = r.read()
    except Exception as e:
        raise RuntimeError(f"Could not download arduino-cli installer: {e}")

    env = {**os.environ, "BINDIR": str(target)}
    proc = subprocess.run(["sh"], input=script, env=env, capture_output=True, check=False)
    if proc.returncode != 0 or not binary.exists():
        detail = (proc.stdout + proc.stderr).decode("utf-8", errors="replace").strip()
        raise RuntimeError("arduino-cli installer failed." + (f"\n{detail}" if detail else ""))
    v = _version(binary)
    if v is None:
        raise RuntimeError(f"arduino-cli installed at {binary} but does not run.")
    _console.print(f"[green]Installed[/green] {v}")
    return binary


def ensure_cli(override: Optional[Path] = None) -> Path:
    """Return a usable arduino-cli, installing it if necessary."""
    return find_cli(override) or bootstrap_cli()


def ensure_core(cli: Path, core: str, *, index_url: Optional[str] = None) -> None:
    """Install a board core if it is not already registered.

    ``index_url`` adds a third-party board-manager index first (stm32duino).
    """
    out = subprocess.run([str(cli), "core", "list"], capture_output=True, text=True)
    if core in out.stdout:
        return
    if index_url:
        subprocess.run([str(cli), "config", "init"], capture_output=True, check=False)
        subprocess.run([str(cli), "config", "add", "board_manager.additional_urls", index_url],
                       capture_output=True, check=False)
    _console.print(f"Installing {core} core ...")
    subprocess.run([str(cli), "core", "update-index"], check=True, capture_output=True)
    subprocess.run([str(cli), "core", "install", core], check=True, capture_output=True)
    _console.print(f"[green]{core} core installed[/green]")


def upload(cli: Path, sketch: Path, *, fqbn: str, port: str, verbose: bool = False) -> None:
    """Compile and upload a sketch."""
    if not sketch.exists():
        raise FileNotFoundError(f"Bundled firmware missing: {sketch}")
    _console.print(f"[bold]Flashing[/bold] [cyan]{sketch.name}[/cyan] on [cyan]{port}[/cyan] ({fqbn})")
    cmd = [str(cli), "compile", "--upload", "--port", port, "--fqbn", fqbn, str(sketch)]
    if verbose:
        cmd.insert(1, "-v")
    if subprocess.run(cmd).returncode != 0:
        raise RuntimeError("arduino-cli flash failed")
    _console.print("[bold green]Flash complete.[/bold green]")
