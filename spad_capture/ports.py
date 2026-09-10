"""Finding a board by USB vendor id, identically on every platform.

Matching on the vendor id rather than on the first ``/dev/ttyACM*`` or
``/dev/cu.usbmodem*`` is what makes this correct when several boards are
attached at once: device paths carry no identity and their order is arbitrary.
"""

from __future__ import annotations

import sys
from typing import Optional

import serial.tools.list_ports as _list_ports_mod
from serial.tools.list_ports_common import ListPortInfo

_PATH_HINT = {"linux": "/dev/ttyACM*", "darwin": "/dev/cu.usbmodem*"}


class PortNotFoundError(RuntimeError):
    """No serial port matched the board being looked for."""


def list_ports() -> list[ListPortInfo]:
    """Every serial port the system reports."""
    return list(_list_ports_mod.comports())


def find_by_vid(vid: int) -> Optional[str]:
    """The first port with this USB vendor id, else None."""
    return next((p.device for p in list_ports() if p.vid == vid), None)


def ports_seen(vid: int) -> str:
    """The 'ports seen' block of an abort message, marking any vendor-id match.

    Only USB-ish ports are listed; the ``/dev/ttyS*`` placeholders Linux always
    reports would bury the real ones.
    """
    lines = []
    for p in list_ports():
        if p.vid is None and not (p.description and p.description != "n/a"):
            continue
        ids = f"{p.vid:04x}:{p.pid:04x}" if p.vid is not None and p.pid is not None else "????:????"
        mark = "   <-- matches" if p.vid == vid else ""
        lines.append(f"    {p.device:<22} {ids}  {p.description or ''}{mark}")
    return "\n".join(lines) if lines else "    (none — no USB serial devices detected)"


def require_port(explicit: Optional[str], *, vid: int, board: str) -> str:
    """The port for ``board``, or one precise error naming what was expected."""
    if explicit:
        if any(p.device == explicit for p in list_ports()):
            return explicit
        raise PortNotFoundError(
            f"Requested port {explicit} not found.\n"
            f"  Ports seen:\n{ports_seen(vid)}\n"
            f"  Fix: pass an existing --port, or plug in the {board}."
        )
    found = find_by_vid(vid)
    if found:
        return found
    plat = "linux" if sys.platform.startswith("linux") else sys.platform
    raise PortNotFoundError(
        f"No {board} found.\n"
        f"  Expected: a serial port with USB VID 0x{vid:04x},\n"
        f"            typically {_PATH_HINT.get(plat, 'a USB serial port')}.\n"
        f"  Ports seen:\n{ports_seen(vid)}\n"
        f"  Fix: plug in the {board}, or pass --port explicitly."
    )
