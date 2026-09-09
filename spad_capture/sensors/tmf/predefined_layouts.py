"""Per-ZoneMode SPAD-coverage tables for the predefined maps.

Source of truth
===============

Every layout in this module is read back from the device after
configuring the firmware for the given ``spad_map_id``. The probe sequence
is:

    configure(spad_map_id), then load the SPAD-1 page (cmd 0x17) and read
    registers 0x24..0xDF. Decode ``enableSpad[10]``, ``tdcChannel[18]``,
    ``tdcChannelSelect``, and ``xOffset_2 / yOffset_2 / xSize / ySize``.
    For time-multiplexed maps, also load the SPAD-2 page (cmd 0x18) for
    sub-capture 1.

The decoded SPAD-to-channel assignment is then translated into the 12-row
x 18-col AMS visual frame used by Figures 30/31/32. The cached result
lives in ``predefined_layouts_data.LAYOUTS``, one entry per predefined
``spad_map_id``.

Coordinate system
=================

Coordinates are scene point-of-view in the 12x18 AMS visual frame:

- ``row 0 = top of scene`` (= silicon-y 14)
- ``col 0 = left of scene`` (= silicon-x 8 inside the 18-col addressable
  window)
- Maps are centered at the lens center. Macro-extension rows 0 and 11 fall
  outside an ``ySize=10`` non-shifted mask (unsampled in the AMS figures).
- Narrower maps occupy a centered sub-rectangle within the 12x18 frame.

The mapping from silicon to visual coords:

    visual_row = 14 - silicon_y        (high silicon-y is the top of scene)
    visual_col = silicon_x - 8         (silicon-x 8..25 maps to visual
                                        0..17; low silicon-x is the left)

This is the same coordinate convention as Figures 30/31/32: a zone shown
at visual ``(row, col)`` in the GUI Mask tab sits at the same position as
that zone in the AMS datasheet figure.
"""

from __future__ import annotations

from spad_capture.sensors.tmf.predefined_layouts_data import LAYOUTS

# Per zone_mode metadata used by the Mask-tab badge: (xSize, ySize, FoV_h, FoV_v).
_FOV_BY_MODE = {
    "3x3_narrow":      (14, 6, 33.0, 32.0),
    "3x3_macro":       (14, 9, 33.0, 47.0),
    "3x3_macro_v2":    (14, 9, 33.0, 47.0),
    "4x4_narrow":      (14, 9, 33.0, 47.0),
    "4x4_narrow_v2":   (14, 9, 33.0, 47.0),
    "3x3_wide":        (18, 10, 41.0, 52.0),
    "4x4_wide":        (18, 10, 41.0, 52.0),
    "9_zone":          (14, 9, 33.0, 47.0),
    "9_zone_v2":       (14, 9, 33.0, 47.0),
    "3x6":             (18, 12, 33.0, 60.0),
    "3x3_checker":     (14, 6, 33.0, 32.0),
    "3x3_checker_rev": (14, 6, 33.0, 32.0),
    "4x4_narrow_v3":   (18, 8, 33.0, 42.0),
    "8x8":             (18, 8, 41.0, 52.0),     # ROM-derived; partial probe data only
}


def resolved_zones_for(zone_mode_value: str) -> list[dict]:
    """Return the per-zone SPAD coverage for a predefined ``ZoneMode``.

    Each dict has ``{zone_id, channel, sub_capture, spads, is_dummy,
    output_pos}`` in the 12x18 AMS visual frame. The order matches the
    device's output-channel stream.
    """
    if zone_mode_value not in LAYOUTS:
        return []
    raw = LAYOUTS[zone_mode_value]
    zones = []
    for z in raw["zones"]:
        zones.append({
            "zone_id": z["zone_id"],
            "channel": z["channel"],
            "sub_capture": z.get("sub_capture", 0),
            "spads": [list(s) for s in z["spads"]],
            "is_dummy": False,
            "output_pos": _compute_output_pos(zone_mode_value, z["zone_id"]),
        })
    return zones


def fov_for(zone_mode_value: str) -> tuple[float, float] | None:
    """Return ``(fov_h_deg, fov_v_deg)`` for a predefined mode, or ``None``.

    Lookup helper exposed for the live viz; the value is *not* persisted
    into capture metadata because it is a static device-spec fact tied
    to ``zone_mode``.
    """
    fov = _FOV_BY_MODE.get(zone_mode_value)
    return (fov[2], fov[3]) if fov else None


def _compute_output_pos(zone_mode: str, zone_id_1based: int) -> list[int]:
    """Output histogram-grid position (row, col) for a given zone id.

    For predefined modes, sensor._assemble_frame reshapes the stitched
    channel stream row-major into (H, W). So zone n maps to
    ((n-1) // W, (n-1) % W) where W is the grid's width.
    """
    grid_shape = {
        "3x3_narrow": (3, 3), "3x3_macro": (3, 3), "3x3_macro_v2": (3, 3),
        "4x4_narrow": (4, 4), "4x4_narrow_v2": (4, 4),
        "3x3_wide": (3, 3),   "4x4_wide": (4, 4),
        "9_zone": (3, 3), "9_zone_v2": (3, 3),
        "3x6": (3, 6),
        "3x3_checker": (3, 3), "3x3_checker_rev": (3, 3),
        "4x4_narrow_v3": (4, 4),
        "8x8": (8, 8),
    }
    h, w = grid_shape.get(zone_mode, (1, max(1, zone_id_1based)))
    n = zone_id_1based - 1
    return [n // w, n % w]


# ---------------------------------------------------------------------------
# Unified zone-meta helper used by both live captures and replay viz so the
# frontend reads a single shape regardless of mode.
# ---------------------------------------------------------------------------


# Custom and predefined masks both use the AMS 12-row × 18-col visual frame
# directly. Scene_row in mask.py YAML == visual_row here. No shift needed.


def build_zone_meta(
    zone_mode_value: str,
    mask_dict: dict | None,
    sensor_layout: dict | None = None,
) -> dict:
    """Produce a frontend-ready zone-meta blob for the live dashboard.

    Returns a dict containing:

    - ``zone_mode``: the mode string.
    - ``resolved_zones``: list of zone dicts in scene point-of-view, with
      ``{output_index, zone_id, channel, sub_capture, is_dummy, spads,
      output_pos}``.
    - ``mask_zones``: legacy alias for the frontend.
    - ``output_labels``: per-output-index labels for the live grid.
    """
    out: dict = {"zone_mode": zone_mode_value}

    # Source of truth in priority order:
    # 1) live sensor layout (preferred, reflects exact device state)
    # 2) saved-capture sensor_layout
    # 3) derive from zone_mode + mask_dict
    resolved = None
    if sensor_layout and isinstance(sensor_layout, dict):
        resolved = sensor_layout.get("resolved_zones")

    if not resolved:
        if zone_mode_value == "custom" and mask_dict:
            try:
                from spad_capture.sensors.tmf.mask import CustomMask, preview as _preview, validate as _validate
                m = CustomMask(**mask_dict)
                v = _validate(m)
                out["mask_preview"] = _preview(m)
                resolved = []
                for i, rz in enumerate(v.resolved):
                    # mask.py spads are already in the AMS 12x18 visual frame.
                    resolved.append({
                        "output_index": i,
                        "zone_id": rz.zone_id,
                        "channel": rz.channel,
                        "is_dummy": rz.is_dummy,
                        "spads": [list(s) for s in rz.spads],
                        "output_pos": [0, i],
                        "sub_capture": rz.sub_capture,
                    })
            except Exception as e:
                out["mask_preview"] = f"(mask invalid: {e})"
                resolved = []
        else:
            pre_zones = resolved_zones_for(zone_mode_value)
            resolved = [
                {
                    "output_index": i,
                    "zone_id": z["zone_id"],
                    "channel": z["channel"],
                    "is_dummy": False,
                    "spads": [list(s) for s in z["spads"]],
                    "output_pos": list(z["output_pos"]),
                    "sub_capture": z.get("sub_capture", 0),
                }
                for i, z in enumerate(pre_zones)
            ]

    out["resolved_zones"] = resolved
    out["mask_zones"] = [
        {
            "id": z["zone_id"],
            "channel": z["channel"],
            "is_dummy": z["is_dummy"],
            "spads": z["spads"],
            "sub_capture": z.get("sub_capture", 0),
        }
        for z in resolved
    ]
    out["output_labels"] = [
        {
            "index": z["output_index"],
            "user_zone_id": z["zone_id"] if not z["is_dummy"] else None,
            "label": ("dummy pixel" if z["is_dummy"] else f"zone {z['zone_id']}"),
            "is_dummy": z["is_dummy"],
        }
        for z in resolved
    ]
    return out
