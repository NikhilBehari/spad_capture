"""Compile and upload the bundled TMF8828 sketch."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from spad_capture import arduino
from spad_capture.sensors.tmf.firmware import sketch_path
from spad_capture.sensors.tmf.sensor import find_arduino_port

_FQBN = "arduino:avr:uno"
_CORE = "arduino:avr"


def flash(port: Optional[str] = None, *, arduino_cli: Optional[Path] = None,
          verbose: bool = False) -> None:
    """Compile and upload the bundled TMF8828 sketch to the Arduino."""
    port = port or find_arduino_port()
    cli = arduino.ensure_cli(arduino_cli)
    arduino.ensure_core(cli, _CORE)
    arduino.upload(cli, sketch_path(), fqbn=_FQBN, port=port, verbose=verbose)
