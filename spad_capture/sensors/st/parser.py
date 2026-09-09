"""CNH wire frame parser and the :class:`Frame` dataclass.

The MCU emits one binary packet per CNH histogram frame. The on-wire layout
(little-endian) is::

    offset  type            field
    ------- --------------- ------------------------------------------
    0       u8              START_BYTE (0xAA)
    1       4s              MAGIC ("STH1")
    5       u32             frame_index (monotonic from MCU)
    9       u32             device_ts_ms (MCU millis() at emit)
    13      u8              mode_code (1 = 4x4, 2 = 8x8)
    14      u16             height (H)
    16      u16             width  (W)
    18      u16             num_bins (B)
    20      f32 * (H*W*B)   histogram, zone-major (z = r*W + c) then bin
    ...     f32 * (H*W)     ambient, one per zone (zone-major)
    ...     u32             crc32 over bytes [1 .. crc_start)
    ...     u8              END_BYTE (0x55)

The decoded per-zone CNH magnitude (exponent-mantissa) is computed on the
MCU, so histogram values arrive here as plain float32.
"""

from __future__ import annotations

import struct
import time
import zlib
from typing import Optional

import numpy as np

from spad_capture.frame import Frame
from spad_capture.sensors.st.config import END_BYTE, MAGIC, START_BYTE


class FrameError(RuntimeError):
    """A framed packet failed validation (sync, magic, crc, shape, length)."""


# start, magic, index, dev_ts, mode_code, H, W, B
_FRAME_HEADER = struct.Struct("<B4sIIBHHH")
_CRC = struct.Struct("<I")

# mode_code -> zone count, for cross-checking the declared H*W.
_MODE_ZONES = {1: 16, 2: 64}


def parse_frame(raw: bytes, *, recv_timestamp: float) -> Frame:
    """Parse ONE complete framed packet (START..END) into a :class:`Frame`.

    Validates the sync byte, magic, terminator, crc32, and that the declared
    lengths fit ``raw`` and match the mode. Raises :class:`FrameError` on any
    mismatch.
    """
    if len(raw) < _FRAME_HEADER.size + _CRC.size + 1:
        raise FrameError(f"frame too short: {len(raw)} bytes")

    start, magic, index, dev_ts, mode_code, h, w, b = _FRAME_HEADER.unpack_from(raw, 0)
    if start != START_BYTE:
        raise FrameError(f"bad start byte 0x{start:02X} (expected 0x{START_BYTE:02X})")
    if magic != MAGIC:
        raise FrameError(f"bad magic {magic!r} (expected {MAGIC!r})")
    if mode_code not in _MODE_ZONES:
        raise FrameError(f"unknown mode_code {mode_code}")
    max_zones = _MODE_ZONES[mode_code]
    if not (1 <= h * w <= max_zones):
        raise FrameError(
            f"H*W={h * w} out of range for mode_code {mode_code} "
            f"(must be 1..{max_zones} output zones)"
        )
    if not (1 <= b <= 255):
        raise FrameError(f"num_bins={b} out of range (must be 1..255)")

    hist_count = h * w * b
    amb_count = h * w
    hist_bytes = hist_count * 4
    amb_bytes = amb_count * 4
    hist_off = _FRAME_HEADER.size
    amb_off = hist_off + hist_bytes
    crc_off = amb_off + amb_bytes
    end_off = crc_off + _CRC.size

    if len(raw) != end_off + 1:
        raise FrameError(
            f"length mismatch: have {len(raw)} bytes, expected {end_off + 1} "
            f"for H={h} W={w} B={b}"
        )
    if raw[end_off] != END_BYTE:
        raise FrameError(f"bad end byte 0x{raw[end_off]:02X} (expected 0x{END_BYTE:02X})")

    (crc_declared,) = _CRC.unpack_from(raw, crc_off)
    crc_actual = zlib.crc32(raw[1:crc_off]) & 0xFFFFFFFF
    if crc_declared != crc_actual:
        raise FrameError(f"crc mismatch: declared 0x{crc_declared:08X}, computed 0x{crc_actual:08X}")

    histogram = (
        np.frombuffer(raw, dtype="<f4", count=hist_count, offset=hist_off)
        .reshape(h, w, b)
        .astype(np.float32)
    )
    ambient = (
        np.frombuffer(raw, dtype="<f4", count=amb_count, offset=amb_off)
        .reshape(h, w)
        .astype(np.float32)
    )
    return Frame(
        index=int(index),
        timestamp=recv_timestamp,
        extras={"mode": "4x4" if mode_code == 1 else "8x8"},
        histogram=histogram,
        ambient=ambient,
        device_ts_ms=int(dev_ts),
    )


def read_and_parse(transport, *, expected: Optional[dict] = None) -> Optional[Frame]:
    """Read one raw packet from ``transport`` and parse it into a :class:`Frame`.

    Returns ``None`` when the transport yields no bytes (stop flag / EOF), so a
    clean stop is distinguished from a corrupt frame. ``expected`` is
    :meth:`Config.resolved` and, when given, asserts the frame's mode and
    ``num_bins`` match the configured geometry. Raises :class:`FrameError` on a
    validation mismatch.
    """
    raw = transport.read_frame()
    if not raw:                                  # stop flag set / EOF — not corruption
        return None
    frame = parse_frame(raw, recv_timestamp=time.time())
    frame.device_index = frame.index             # MCU per-ranging counter
    frame.index = transport.next_index()         # host monotonic session counter
    if expected is not None:
        if frame.extras["mode"] != expected["mode"]:
            raise FrameError(
                f"mode mismatch: frame={frame.extras['mode']}, expected={expected['mode']}"
            )
        got_bins = frame.histogram.shape[2]
        if got_bins != expected["num_bins"]:
            raise FrameError(
                f"num_bins mismatch: frame={got_bins}, expected={expected['num_bins']}"
            )
        got_h, got_w = frame.histogram.shape[:2]
        exp_h = expected.get("output_height")
        exp_w = expected.get("output_width")
        if exp_h is None or exp_w is None:    # pre-ROI descriptor: fall back to full grid
            exp_h, exp_w = {"4x4": (4, 4), "8x8": (8, 8)}[expected["mode"]]
        if (got_h, got_w) != (exp_h, exp_w):
            raise FrameError(
                f"grid shape mismatch: frame=({got_h},{got_w}), "
                f"expected=({exp_h},{exp_w})"
            )
    return frame
