"""User-defined SPAD masks for the TMF8828.

Coordinate convention
=====================

Masks are defined in the AMS 12-row x 18-col visual frame from DS000693
Figures 30/31/32, which is the same frame the datasheet uses to draw every
predefined map. Scene point-of-view: row 0 is the top of the scene, col 0
is the left. The library applies the lens V-flip internally when packing
the on-silicon mask, so user-facing config and viz never expose
sensor-electronic coordinates::

    row  0   x  x  x  x  x  x  x  x  x  x  x  x  x  x  x  x  x  x   macro-extension (top), not selectable
    row  1   .  .  .  .  .  .  .  .  .  .  .  .  .  .  .  .  .  .   single-shot writable
    row  2   .  .  .  .  .  .  .  .  .  .  .  .  .  .  .  .  .  .
    row  3   .  .  .  .  .  .  .  .  .  .  .  .  .  .  .  .  .  .
    row  4   .  .  .  .  .  .  .  .  .  .  .  .  .  .  .  .  .  .
    row  5   .  .  .  .  .  .  .  .  .  .  .  .  .  .  .  .  .  .
    row  6   .  .  .  .  .  .  .  .  .  .  .  .  .  .  .  .  .  .   end of single-shot band
    row  7   .  .  .  .  .  .  .  .  .  .  .  .  .  .  .  .  .  .   time-mux band (auto-promoted)
    row  8   .  .  .  .  .  .  .  .  .  .  .  .  .  .  .  .  .  .
    row  9   .  .  .  .  .  .  .  .  .  .  .  .  .  .  .  .  .  .
    row 10   .  .  .  .  .  .  .  .  .  .  .  .  .  .  .  .  .  .   end of mask region
    row 11   x  x  x  x  x  x  x  x  x  x  x  x  x  x  x  x  x  x   macro-extension (bottom)
            col 0                                                col 17

User-addressable rows: **1..10** (the 10 device mask rows). They split
into:

- **Single-shot band (rows 1..6).** Writable in both ``map_id=14`` (faster)
  and ``map_id=15``. Most masks live here.
- **Time-mux band (rows 7..10).** Writable only in ``map_id=15``. The
  library auto-promotes to time-multiplexed mode when any zone touches
  this band.

Rows 0 and 11 are macro-extension placeholders. They correspond to silicon
rows above and below the 10-row mask region and are not selectable with
the default ``y_offset_2 = 0``.

The single-shot vs. time-mux split is a device-side constraint of the
TMF8828's SPAD-1 page register window, not a lens-projection limitation.

Schema
======

A mask is supplied as either a list of zones or an ASCII grid::

    mask:
      zones:
        - id: 1
          spads: [[6, 8], [6, 9]]      # central 2-SPAD zone (visual row 6)

    # Equivalent grid form. Digit = zone id; '.' = free SPAD; 'x' = macro.
    mask:
      grid: |
        x x x x x x x x x x x x x x x x x x   # row 0 macro
        . . . . . . . . . . . . . . . . . .   # row 1
        ...
        . . . . . . . . 1 1 . . . . . . . .   # row 6 (single-shot, near center)
        ...
        x x x x x x x x x x x x x x x x x x   # row 11 macro

Rules enforced (sources: DS000693 §7.4.1; AMS validator
``tmf8x2x_spad_mask_tool.c``):

- Each user zone requires at least 2 enabled SPADs that touch, orthogonal
  or diagonal.
- 1x1 single-SPAD masks are rejected.
- The device requires at least one zone in each TDC pair ``(2|3, 4|5, 6|7,
  8|9)`` per sub-capture so that electrical calibration completes. The
  library satisfies this transparently: when a mask has fewer than 4 zones
  in a sub-capture, dummy pixels are appended in unused corners. Dummies
  carry ``zone_id = 0`` and appear at the tail of the saved-data zone axis.
- Time-multiplexed masks require each zone to fit entirely within one
  sub-capture band (rows 1..6 or rows 7..10). Zones that straddle the
  boundary are rejected.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Optional

from pydantic import BaseModel, Field, model_validator

# --- physical constants (AMS tmf8x2x_includes.h + DS000693 Fig 30/31) --------
#
# Coordinate system: user-facing coords are the AMS 12x18 visual frame.
# This is the same frame Figures 30/31/32 of the datasheet draw all
# predefined maps inside, so a YAML "row" is the same row that appears in
# the AMS figure.
#
#   Row  0:    silicon-y 14. Macro-extension upper band (above the 10-row
#              mask region with yOffset_2=0; not user-addressable in this
#              build).
#   Rows 1..10: silicon-y 13..4. The 10 mask rows (mask-y 9..0 after V-flip).
#   Row 11:    silicon-y 3. Macro-extension lower band (below the mask region).
#
# Device-side writable region per map_id:
#
#   map_id=14 (single-shot, cid_rid=0x17): SPAD-1 accepts writes to mask-y
#     4..9 (visual rows 1..6) only. Rows 7..10 are silently kept at device
#     defaults; zones cannot be placed there in single-shot mode.
#   map_id=15 (time-mux, cid_rid=0x17 and 0x18): SPAD-1 accepts mask-y 4..9
#     as before; SPAD-2 accepts all mask-y 0..9 (visual rows 1..10). Zones
#     are split across the two sub-capture pages to cover all 10 rows.

ADDRESSABLE_W = 18                # 18 columns of SPADs (cols 0..17)
ADDRESSABLE_H = 12                # 12-row visual frame (rows 0..11)
VISUAL_ROW_MASK_TOP = 1           # first user-addressable row (mask-y 9)
VISUAL_ROW_MASK_BOTTOM = 10       # last user-addressable row (mask-y 0)
SINGLE_SHOT_MAX_VISUAL_ROW = 6    # rows 1..6 fit single-shot (map_id=14)

MASK_Y_SIZE = 10                  # ySize byte sent to device
_Y_OFFSET_2 = 0                   # mask centered on silicon (yLLC = 4)
_ENABLE_ROW_COUNT = 10            # wire format always has 10 rows × 3 bytes
NUMBER_OF_CHANNELS = 10
USER_CHANNELS: tuple[int, ...] = (2, 3, 4, 5, 6, 7, 8, 9)
TDC_PAIRS: tuple[tuple[int, int], ...] = ((2, 3), (4, 5), (6, 7), (8, 9))


def user_row_to_mask_y(visual_row: int) -> int:
    """Visual-frame row → device mask-y. visual_row 1..10 maps to mask-y 9..0
    via the V-flip (AMS Fig 30/31: high silicon-y = top of scene).
    Rows 0 and 11 are macro-extension and not user-addressable."""
    return VISUAL_ROW_MASK_BOTTOM - visual_row + VISUAL_ROW_MASK_TOP - 1 + MASK_Y_SIZE - 1 - (VISUAL_ROW_MASK_BOTTOM - VISUAL_ROW_MASK_TOP)


# Simpler closed-form: mask_y = 10 - visual_row  (valid for visual_row 1..10)
def _row_to_mask_y(r: int) -> int:
    return MASK_Y_SIZE - r           # visual_row 1 → mask-y 9 ; row 10 → mask-y 0


# Calibration-dummy SPAD positions, one 2-SPAD pair per visual-frame corner.
# Single-shot dummies live in rows 1..6 (writable for map_id=14). Time-mux
# dummies for sub-capture 1 live in rows 7..10 (only writable via SPAD-2).
# Order matters: the resolver assigns channels 2, 4, 6, 8 in this order.
_DUMMY_CORNERS_SINGLE: tuple[tuple[tuple[int, int], tuple[int, int]], ...] = (
    ((6, 0), (6, 1)),         # bottom-left of single-shot range  → ch 2
    ((6, 16), (6, 17)),       # bottom-right                       → ch 4
    ((1, 0), (1, 1)),         # top-left                           → ch 6
    ((1, 16), (1, 17)),       # top-right                          → ch 8
)
_DUMMY_CORNERS_TIMEMUX_SUB1: tuple[tuple[tuple[int, int], tuple[int, int]], ...] = (
    ((10, 0), (10, 1)),       # row 10 left  (reachable only via SPAD-2 page)
    ((10, 16), (10, 17)),     # row 10 right
    ((7, 0), (7, 1)),
    ((7, 16), (7, 17)),
)

# I2C wire-format constants (TMF8X2X common config page; offsets are from 0x24).
# Layout: enableSpad[10] × 3 B + tdcChannel[18] × 4 B + tdcChannelSelect × 3 B
# + xOffset_2 + yOffset_2 + xSize + ySize → 30 + 72 + 3 + 4 = 109 bytes.
_WIRE_BYTES = 109
_OFF_ENABLE = 0      # 0x24
_OFF_TDC = 30        # 0x42
_OFF_TDCSEL = 102    # 0x8a
_OFF_XOFFSET = 105   # 0x8d
_OFF_YOFFSET = 106   # 0x8e
_OFF_XSIZE = 107     # 0x8f
_OFF_YSIZE = 108     # 0x90


# --- public types ------------------------------------------------------------


class Zone(BaseModel):
    """A user-defined zone: one histogram, one or more enabled SPADs."""

    id: int = Field(ge=1, description="Zone number (1 = first zone).")
    spads: list[tuple[int, int]] = Field(
        ...,
        description="(row, col) in the AMS 12-row × 18-col visual frame "
                    "(scene POV, matching DS000693 Figures 30/31/32). "
                    "Row 0 = top of scene, col 0 = left of scene. "
                    "Rows 1..10 are user-addressable; rows 0 and 11 are "
                    "macro-extension and not selectable.",
    )

    @model_validator(mode="after")
    def _no_duplicate_spads(self) -> "Zone":
        if len(self.spads) != len({tuple(s) for s in self.spads}):
            raise ValueError(f"Zone {self.id}: duplicate SPAD coordinate.")
        return self


class CustomMask(BaseModel):
    """Top-level mask configuration. Supply either ``zones`` or ``grid``.

    The library auto-detects whether map_id=14 (single-shot) or map_id=15
    (time-multiplexed) is needed based on the zones' row range:
      - all zones in rows 1..6 → map_id=14 (faster)
      - any zone in rows 7..10 → map_id=15 (full 10-row coverage)
    """

    zones: Optional[list[Zone]] = None
    grid: Optional[str] = None

    @model_validator(mode="after")
    def _normalize(self) -> "CustomMask":
        if self.zones is None and self.grid is None:
            raise ValueError("CustomMask requires either `zones` or `grid`.")
        zones_from_grid = parse_grid(self.grid) if self.grid else None
        if self.zones is None:
            self.zones = zones_from_grid
        elif zones_from_grid is not None:
            if _zones_to_canon(self.zones) != _zones_to_canon(zones_from_grid):
                raise ValueError("`zones` and `grid` disagree.")
        return self


@dataclass(frozen=True)
class ResolvedZone:
    """Internal post-resolution form: zone with its assigned TDC channel."""
    zone_id: int                 # user-supplied id, or 0 for an auto-dummy
    channel: int                 # TDC channel (2..9)
    spads: tuple[tuple[int, int], ...]
    is_dummy: bool = False       # True when added by the resolver for calibration
    sub_capture: int = 0         # 0 = SPAD-1 page (single-shot or sub-cap 0 of time-mux);
                                 # 1 = SPAD-2 page (sub-cap 1 of time-mux only)


@dataclass
class ValidationResult:
    """Outcome of a CustomMask check. Successful when ``ok`` is True."""
    ok: bool
    errors: list[str]
    user_zones: list[Zone]              # the zones the user supplied
    resolved: list[ResolvedZone]        # user + auto-dummy zones, with channels assigned
    bbox: tuple[int, int, int, int]     # (min_row, min_col, max_row, max_col)
    x_size: int
    y_size: int
    time_multiplexed: bool = False      # True if mask requires map_id=15


# --- ASCII grid parser -------------------------------------------------------


def parse_grid(grid_text: str) -> list[Zone]:
    """Parse an ASCII grid where each non-dot cell is a digit naming a zone id.

    The grid is the AMS 12-row x 18-col visual frame from DS000693 Figures
    30/31/32 (the same row and column convention used everywhere in this
    codebase).

    - Row 0 (top): upper macro-extension band, not user-addressable. Mark
      with ``x``.
    - Rows 1..6: single-shot writable (``map_id=14``). Use freely.
    - Rows 7..10: writable only in time-multiplexed mode (``map_id=15``).
      The library auto-promotes to time-mux when any zone touches rows
      7..10.
    - Row 11 (bottom): lower macro-extension band, not user-addressable.
      Mark with ``x``.

    Symbols:

    - ``1`` .. ``9``   user zone digit (row must be 1..10)
    - ``.``            free SPAD (selectable but not chosen)
    - ``x``            unselectable SPAD (rows 0 and 11, macro extension)
    - ``_`` / ``-``    aliases for ``.``

    Whitespace between cells is tolerated. Up to 12 rows are accepted.
    """
    raw_lines = grid_text.splitlines()
    rows = []
    for ln in raw_lines:
        # Strip trailing line comments (everything from '#' onward).
        if "#" in ln:
            ln = ln[:ln.index("#")]
        if ln.strip():
            rows.append(ln)
    if not rows:
        raise ValueError("Empty grid.")
    by_id: dict[int, list[tuple[int, int]]] = {}
    for r, line in enumerate(rows):
        cells = [ch for ch in line if not ch.isspace()]
        if r >= ADDRESSABLE_H:
            raise ValueError(f"Grid has {len(rows)} rows; max is {ADDRESSABLE_H}.")
        if len(cells) > ADDRESSABLE_W:
            raise ValueError(
                f"Grid row {r} has {len(cells)} cells; max is {ADDRESSABLE_W}."
            )
        for c, ch in enumerate(cells):
            if ch in (".", "_", "-", "x"):
                continue
            if not (ch.isdigit() and 1 <= int(ch) <= 9):
                raise ValueError(
                    f"Grid row {r} col {c}: invalid cell {ch!r} "
                    f"(use '.' free, 'x' inactive, digits 1..9 for zone id)."
                )
            by_id.setdefault(int(ch), []).append((r, c))
    return [Zone(id=zid, spads=spads) for zid, spads in sorted(by_id.items())]


def _zones_to_canon(zones: list[Zone]) -> set[tuple[int, int, int]]:
    return {(z.id, r, c) for z in zones for (r, c) in z.spads}


# --- resolver: zone IDs → TDC channels (+ auto-dummies) ----------------------


def _adjacent(a: tuple[int, int], b: tuple[int, int]) -> bool:
    if a == b:
        return False
    return abs(a[0] - b[0]) <= 1 and abs(a[1] - b[1]) <= 1


def _has_adjacent_pair(spads: list[tuple[int, int]]) -> bool:
    return any(_adjacent(a, b) for i, a in enumerate(spads) for b in spads[i + 1:])


def _needs_time_mux(zones: list[Zone]) -> bool:
    """True if any user zone places a SPAD at visual_row > SINGLE_SHOT_MAX_VISUAL_ROW.
    Single-shot map_id=14 covers visual rows 1..6 only; rows 7..10 need
    time-mux (map_id=15) which writes the SPAD-2 page."""
    return any(r > SINGLE_SHOT_MAX_VISUAL_ROW for z in zones for (r, _) in z.spads)


def _resolve_one_subcap(
    zones: list[Zone],
    *,
    sub_capture: int,
    dummy_corners: tuple,
    seen_global: dict[tuple[int, int], int],
) -> tuple[list[ResolvedZone], list[str]]:
    """Assign channels + dummies for one sub-capture's worth of zones.

    Each sub-capture has its own 8-channel TDC: channels 2..9. Each
    sub-cap needs at least one zone per TDC pair for electrical calibration.
    """
    errors: list[str] = []
    primary = [2, 4, 6, 8]
    secondary = [3, 5, 7, 9]
    channel_pool = primary + secondary

    user_zones = sorted(zones, key=lambda z: z.id)
    if len(user_zones) > len(channel_pool):
        errors.append(
            f"Sub-capture {sub_capture} has {len(user_zones)} user zones; "
            f"the device supports max {len(channel_pool)} per sub-capture."
        )
        return [], errors

    seen_spads: dict[tuple[int, int], int] = dict(seen_global)
    for z in user_zones:
        for rc in z.spads:
            if rc in seen_spads and seen_spads[rc] != z.id:
                errors.append(
                    f"SPAD {rc} is assigned to zone {seen_spads[rc]} AND zone {z.id}."
                )
            seen_spads[rc] = z.id

    resolved: list[ResolvedZone] = []
    used_channels: set[int] = set()
    for z, ch in zip(user_zones, channel_pool):
        resolved.append(ResolvedZone(z.id, ch, tuple(z.spads), is_dummy=False, sub_capture=sub_capture))
        used_channels.add(ch)

    dummy_corner_iter = iter(dummy_corners)
    for pair in TDC_PAIRS:
        if not (used_channels & set(pair)):
            try:
                corner = next(c for c in dummy_corner_iter if all(rc not in seen_spads for rc in c))
            except StopIteration:
                errors.append(
                    f"Sub-capture {sub_capture}: TDC pair {pair} is empty and no free "
                    f"corner is available for an automatic dummy. Add a zone in this "
                    f"sub-capture's row range covering channel {pair[0]} or {pair[1]}."
                )
                continue
            for rc in corner:
                seen_spads[rc] = 0
            resolved.append(ResolvedZone(0, pair[0], tuple(corner), is_dummy=True, sub_capture=sub_capture))
            used_channels.add(pair[0])

    # Update the global seen-spads map so the other sub-capture knows what's taken.
    seen_global.update(seen_spads)
    return resolved, errors


def _resolve(zones: list[Zone]) -> tuple[list[ResolvedZone], list[str], bool]:
    """Map user zone IDs to (sub-capture, TDC channel) pairs and add any
    calibration dummies needed.

    Returns ``(resolved_zones, errors, time_multiplexed)``. When any user
    zone places a SPAD at visual_row > SINGLE_SHOT_MAX_VISUAL_ROW (= 6),
    the mask is promoted to time-multiplexed mode (map_id=15) and zones are
    split across two sub-captures by row range:
      - sub-cap 0 (SPAD-1 page, writable mask-y 4..9): zones with all
        SPADs in visual rows 1..6
      - sub-cap 1 (SPAD-2 page, writable mask-y 0..9 = full range):
        zones with any SPAD in scene rows 6..9
    """
    user_zones = list(zones)
    time_mux = _needs_time_mux(user_zones)

    if not time_mux:
        seen: dict[tuple[int, int], int] = {}
        resolved, errs = _resolve_one_subcap(
            user_zones, sub_capture=0,
            dummy_corners=_DUMMY_CORNERS_SINGLE, seen_global=seen,
        )
        return resolved, errs, False

    # Time-mux: split zones by visual-row range. A zone goes to sub-cap 1
    # if any of its SPADs is at visual_row > SINGLE_SHOT_MAX_VISUAL_ROW.
    # Zones that straddle the row-6/row-7 boundary are rejected so the
    # split is unambiguous (each zone lives in exactly one sub-capture).
    sub0_zones: list[Zone] = []
    sub1_zones: list[Zone] = []
    errors: list[str] = []
    for z in user_zones:
        rows = {r for (r, _) in z.spads}
        if all(r <= SINGLE_SHOT_MAX_VISUAL_ROW for r in rows):
            sub0_zones.append(z)
        elif all(r > SINGLE_SHOT_MAX_VISUAL_ROW for r in rows):
            sub1_zones.append(z)
        else:
            errors.append(
                f"Zone {z.id} has SPADs in BOTH the top (rows 1..{SINGLE_SHOT_MAX_VISUAL_ROW}) "
                f"and bottom (rows {SINGLE_SHOT_MAX_VISUAL_ROW + 1}..{VISUAL_ROW_MASK_BOTTOM}) "
                f"sub-captures. Time-multiplexed masks require each zone to fit entirely "
                f"in one sub-capture. Split it into two zones or move SPADs to one side."
            )

    seen: dict[tuple[int, int], int] = {}
    sub0_resolved, e0 = _resolve_one_subcap(
        sub0_zones, sub_capture=0,
        dummy_corners=_DUMMY_CORNERS_SINGLE, seen_global=seen,
    )
    sub1_resolved, e1 = _resolve_one_subcap(
        sub1_zones, sub_capture=1,
        dummy_corners=_DUMMY_CORNERS_TIMEMUX_SUB1, seen_global=seen,
    )
    errors.extend(e0)
    errors.extend(e1)

    return sub0_resolved + sub1_resolved, errors, True


# --- validator ---------------------------------------------------------------


def validate(mask: CustomMask) -> ValidationResult:
    """Apply the documented TMF882X mask rules. Returns all failures at once."""
    user_zones = list(mask.zones or [])
    errors: list[str] = []

    if not user_zones:
        errors.append("Mask is empty: at least one zone must be defined.")

    if user_zones:
        resolved, resolver_errors, time_mux = _resolve(user_zones)
    else:
        resolved, resolver_errors, time_mux = [], [], False
    errors.extend(resolver_errors)

    # Validate every SPAD lives in the addressable region: visual rows
    # 1..10 (rows 0 and 11 are macro-extension only, not selectable with
    # yOffset_2=0) and cols 0..17.
    for rz in resolved:
        for (r, c) in rz.spads:
            if not (VISUAL_ROW_MASK_TOP <= r <= VISUAL_ROW_MASK_BOTTOM
                    and 0 <= c < ADDRESSABLE_W):
                if r == 0 or r == ADDRESSABLE_H - 1:
                    errors.append(
                        f"SPAD ({r},{c}) is in the macro-extension band "
                        f"(visual rows 0 and 11). Those rows lie above and below "
                        f"the 10-row mask region with yOffset_2=0 and are not "
                        f"selectable. Use rows {VISUAL_ROW_MASK_TOP}..{VISUAL_ROW_MASK_BOTTOM}."
                    )
                else:
                    errors.append(
                        f"SPAD ({r},{c}) is outside the addressable area "
                        f"(rows {VISUAL_ROW_MASK_TOP}..{VISUAL_ROW_MASK_BOTTOM} top-to-bottom, "
                        f"cols 0..{ADDRESSABLE_W - 1} left-to-right)."
                    )

    for z in user_zones:
        if len(z.spads) < 2:
            n = len(z.spads)
            errors.append(
                f"Zone {z.id} has only {n} SPAD{'s' if n != 1 else ''}; minimum is "
                "2 adjacent SPADs."
            )
        elif not _has_adjacent_pair(z.spads):
            errors.append(
                f"Zone {z.id} has {len(z.spads)} SPADs but none are adjacent "
                "(any direction)."
            )

    if resolved:
        rs = [r for rz in resolved for (r, _) in rz.spads]
        cs = [c for rz in resolved for (_, c) in rz.spads]
        bbox = (min(rs), min(cs), max(rs), max(cs))
        x_size = bbox[3] - bbox[1] + 1
        y_size = bbox[2] - bbox[0] + 1
    else:
        bbox = (0, 0, 0, 0)
        x_size = y_size = 0

    return ValidationResult(
        ok=not errors, errors=errors,
        user_zones=user_zones, resolved=resolved,
        bbox=bbox, x_size=x_size, y_size=y_size,
        time_multiplexed=time_mux,
    )


# --- preview -----------------------------------------------------------------


def preview(mask: CustomMask, *, full_area: bool = True) -> str:
    """Render an ASCII visualization of the AMS 12x18 visual frame.

    Convention (matches the YAML grid form and DS000693 Figs 30/31/32):

    - ``1``, ``2``, ...   user zone digit
    - ``.``               free SPAD (selectable but not chosen)
    - ``x``               macro-extension row (rows 0 and 11; not selectable)
    - ``t``               single-shot band (rows 1..6, writable for ``map_id=14``)
    - ``T``               time-mux band (rows 7..10, writable only for ``map_id=15``)

    Auto-added dummy pixels are not drawn. They are internal placeholders
    that the library adds when a sub-capture has fewer than 4 user zones,
    so the device's TDC-pair calibration rule is met. A footer reports how
    many were added and whether the mask was promoted to time-mux mode.
    """
    v = validate(mask)
    user_cells: dict[tuple[int, int], str] = {}
    n_dummy = 0
    for rz in v.resolved:
        if rz.is_dummy:
            n_dummy += 1
            continue
        for rc in rz.spads:
            user_cells[rc] = str(rz.zone_id)

    if full_area:
        cols = list(range(ADDRESSABLE_W))
        rows = list(range(ADDRESSABLE_H))
    elif user_cells:
        rs = [rc[0] for rc in user_cells]
        cs = [rc[1] for rc in user_cells]
        rows = list(range(min(rs), max(rs) + 1))
        cols = list(range(min(cs), max(cs) + 1))
    else:
        return "(empty mask)"

    header = "       " + " ".join(f"{c:2d}" for c in cols)
    lines = [header]
    for r in rows:
        row_cells = []
        for c in cols:
            if (r, c) in user_cells:
                row_cells.append(user_cells[(r, c)])
            elif r < VISUAL_ROW_MASK_TOP or r > VISUAL_ROW_MASK_BOTTOM:
                row_cells.append("x")            # macro-extension row
            else:
                row_cells.append(".")
        lines.append(f"r{r:>2}    " + "  ".join(row_cells))
    lines.append("")
    lines.append("scene POV: row 0 = top, col 0 = left  (matches DS000693 Fig 30/31/32)")
    lines.append("rows 1..6 = single-shot writable      rows 7..10 = time-mux-only")
    lines.append(".  free SPAD     digit  user zone     x  macro-extension (rows 0, 11)")
    if v.time_multiplexed:
        lines.append(f"(auto-promoted to time-multiplexed mode: zones span rows {VISUAL_ROW_MASK_TOP}..{VISUAL_ROW_MASK_BOTTOM})")
    if n_dummy:
        lines.append(
            f"(library will auto-add {n_dummy} dummy pixel zone"
            f"{'s' if n_dummy != 1 else ''} at unused corners; not drawn here, "
            "preserved in the saved data, hidden from the viz)"
        )
    return "\n".join(lines)


# --- byte serializer (109-byte packed I2C payload) ---------------------------


def _encode_channel(channel: int, y_pos: int) -> int:
    """AMS ``TMF8X2X_MAIN_SPAD_ENCODE_CHANNEL`` macro, in Python.

    Packs the 3 bits of ``channel`` into the tdcChannel[col] uint32 at the y
    position's LSB/MID/MSB slots (bits y, 10+y, 20+y). The bit-extraction
    pattern uses (channel & m) >> shift_down to first normalize each bit to
    position 0, then shifts up by the slot offset.
    """
    lsb = (channel & 1)
    mid = (channel & 2) >> 1
    msb = (channel & 4) >> 2
    return (lsb << y_pos) | (mid << (10 + y_pos)) | (msb << (20 + y_pos))


def _pack_subcap(zones: list[ResolvedZone]) -> bytes:
    """Pack one sub-capture's resolved zones into a 109-byte SPAD-page payload."""
    enable_spad = [0] * _ENABLE_ROW_COUNT
    tdc_channel = [0] * ADDRESSABLE_W
    tdc_channel_select = 0

    for rz in zones:
        ch = rz.channel
        ch_use_alt = ch in (8, 9)
        ch_encoded = (ch - 8) if ch_use_alt else ch
        for (r, c) in rz.spads:
            # AMS 12-row visual frame → device mask-y. visual_row 1 = top
            # of mask region (silicon-y 13, mask-y 9). visual_row 10 = bottom
            # (silicon-y 4, mask-y 0). V-flip per Fig 30/31 convention.
            sy = _row_to_mask_y(r)
            sx = c
            enable_spad[sy] |= (1 << sx)
            tdc_channel[sx] |= _encode_channel(ch_encoded, sy)
            if ch_use_alt:
                tdc_channel_select |= (1 << sy)

    buf = bytearray(_WIRE_BYTES)
    for i, e in enumerate(enable_spad):
        buf[_OFF_ENABLE + 3 * i + 0] = e & 0xFF
        buf[_OFF_ENABLE + 3 * i + 1] = (e >> 8) & 0xFF
        buf[_OFF_ENABLE + 3 * i + 2] = (e >> 16) & 0xFF
    struct.pack_into(f"<{ADDRESSABLE_W}I", buf, _OFF_TDC, *tdc_channel)
    buf[_OFF_TDCSEL + 0] = tdc_channel_select & 0xFF
    buf[_OFF_TDCSEL + 1] = (tdc_channel_select >> 8) & 0xFF
    buf[_OFF_TDCSEL + 2] = (tdc_channel_select >> 16) & 0xFF
    struct.pack_into("<bbBB", buf, _OFF_XOFFSET,
                     0, _Y_OFFSET_2, ADDRESSABLE_W, MASK_Y_SIZE)
    return bytes(buf)


def to_bytes(mask: CustomMask) -> bytes:
    """Serialize a single-shot mask (map_id=14) to a 109-byte SPAD-1 payload.

    Only valid for masks where every SPAD is in scene rows 0..5 (the device's
    writable region for cid_rid=0x17 in non-time-mux mode). For masks that
    reach into rows 6..9, use :func:`to_bytes_split` and map_id=15 instead.

    Layout (starting at register ``TMF8X2X_COM_SPAD_ENABLE_SPAD0_0`` = 0x24):

    - ``enableSpad[10]``: 10 entries of 24 bits. One row per entry, bit
      ``x`` is set when the SPAD at silicon ``(y, x)`` is enabled.
    - ``tdcChannel[18]``: 18 entries of 32 bits. One column per entry; the
      3-bit channel is packed at the y position via
      ``TMF8X2X_MAIN_SPAD_ENCODE_CHANNEL``.
    - ``tdcChannelSelect``: 24 bits. Bit ``y`` is set when row ``y`` uses
      channels 8 and 9 (the alternate pair).
    - ``xOffset_2, yOffset_2, xSize, ySize``: 4 bytes.

    AMS Fig 30/31/32 convention: high silicon-y = top of scene, so
    visual_row → silicon_y with a V-flip and visual_col → silicon_x as-is.
    """
    v = validate(mask)
    if not v.ok:
        raise ValueError("Mask is invalid:\n  - " + "\n  - ".join(v.errors))
    if v.time_multiplexed:
        raise ValueError(
            "Mask requires time-multiplexed mode (uses visual rows > 6). "
            "Call to_bytes_split(mask) and upload both sub-captures, or "
            "move zones into rows 0..5 for single-shot mode."
        )
    return _pack_subcap(v.resolved)


def to_bytes_split(mask: CustomMask) -> tuple[bytes, bytes]:
    """Serialize a time-multiplexed mask (map_id=15) into two 109-byte
    payloads: one for the SPAD-1 page (sub-cap 0, scene rows 0..5) and one
    for the SPAD-2 page (sub-cap 1, scene rows 6..9).

    Each sub-capture has its own channel pool (2..9) and its own
    auto-added dummy pixels for the TDC-pair rule. The split is internal:
    the captured frame's output array presents user zones in ascending
    zone-id order across both sub-captures.
    """
    v = validate(mask)
    if not v.ok:
        raise ValueError("Mask is invalid:\n  - " + "\n  - ".join(v.errors))
    sub0 = [rz for rz in v.resolved if rz.sub_capture == 0]
    sub1 = [rz for rz in v.resolved if rz.sub_capture == 1]
    return _pack_subcap(sub0), _pack_subcap(sub1)


def output_shape(mask: CustomMask) -> tuple[int, int]:
    """Histogram grid shape ``(rows, cols)`` for a capture.

    Flat ``(1, N)`` where N is the total number of zones (user plus
    auto-dummies). User zones come first in ascending id order, then
    dummies.
    """
    v = validate(mask)
    if not v.ok:
        raise ValueError("Mask is invalid:\n  - " + "\n  - ".join(v.errors))
    return (1, len(v.resolved))


def zone_index_map(mask: CustomMask) -> dict[int, int]:
    """Return ``{user_zone_id: output_axis_index}`` for the resolved layout."""
    v = validate(mask)
    if not v.ok:
        raise ValueError("Mask is invalid:\n  - " + "\n  - ".join(v.errors))
    out: dict[int, int] = {}
    idx = 0
    for rz in v.resolved:
        if not rz.is_dummy:
            out[rz.zone_id] = idx
        idx += 1
    return out


def channel_for_zone(mask: CustomMask) -> dict[int, int]:
    """Return ``{user_zone_id: tdc_channel}`` after resolution."""
    v = validate(mask)
    if not v.ok:
        raise ValueError("Mask is invalid:\n  - " + "\n  - ".join(v.errors))
    return {rz.zone_id: rz.channel for rz in v.resolved if not rz.is_dummy}
