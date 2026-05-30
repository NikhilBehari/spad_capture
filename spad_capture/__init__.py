"""Capture pipeline for the AMS OSRAM TMF8828 SPAD sensor."""

from spad_capture.config import Config, RangeMode, ZoneMode, load_config
from spad_capture.sensor import Frame, TMF8828Sensor

__version__ = "0.1.0"
__all__ = ["Config", "Frame", "RangeMode", "TMF8828Sensor", "ZoneMode", "load_config"]
