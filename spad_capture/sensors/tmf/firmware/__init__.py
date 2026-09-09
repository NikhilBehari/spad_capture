"""Vendored Arduino firmware for the TMF8828.

The firmware lives in ``tmf8828/`` (a complete copy of AMS's reference
Arduino driver). ``firmware_dir()`` returns the absolute path to the
sketch directory for ``arduino-cli compile --upload``.
"""

from pathlib import Path


def firmware_dir() -> Path:
    """Path to the TMF8828 .ino sketch directory."""
    return Path(__file__).parent / "tmf8828"


def sketch_path() -> Path:
    """Path to the .ino sketch file itself."""
    return firmware_dir() / "tmf8828.ino"
