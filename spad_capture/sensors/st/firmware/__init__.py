"""Vendored stm32duino sketch for the VL53L8CH.

The sketch and the ST VL53LMZ ULD it drives are in ``vl53l8ch_cnh/``.
``sketch_path()`` locates it for ``arduino-cli compile --upload``.
"""

from pathlib import Path


def firmware_dir() -> Path:
    """Path to the VL53L8CH sketch directory."""
    return Path(__file__).parent / "vl53l8ch_cnh"


def sketch_path() -> Path:
    """Path to the .ino sketch file itself."""
    return firmware_dir() / "vl53l8ch_cnh.ino"
