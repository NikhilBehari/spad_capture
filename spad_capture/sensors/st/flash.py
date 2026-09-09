"""Compile and upload the bundled VL53L8CH sketch to the NUCLEO board."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from spad_capture import arduino
from spad_capture.sensors.st.firmware import sketch_path
from spad_capture.sensors.st.transport import require_port

_FQBN = "STMicroelectronics:stm32:Nucleo_64:pnum=NUCLEO_F401RE"
_CORE = "STMicroelectronics:stm32"
_INDEX = ("https://github.com/stm32duino/BoardManagerFiles/raw/main/"
          "package_stmicroelectronics_index.json")


def flash(port: Optional[str] = None, *, arduino_cli: Optional[Path] = None,
          verbose: bool = False) -> None:
    """Compile and upload the bundled VL53L8CH sketch to the NUCLEO-F401RE."""
    port = require_port(port)
    cli = arduino.ensure_cli(arduino_cli)
    arduino.ensure_core(cli, _CORE, index_url=_INDEX)
    arduino.upload(cli, sketch_path(), fqbn=_FQBN, port=port, verbose=verbose)
