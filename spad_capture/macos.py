"""Opening the Realsense on macOS. Every entry point no-ops off macOS.

macOS routes UVC devices through camera assistant daemons that claim the
Realsense the moment anything enumerates it. They run as a system user, respawn
on demand, and cannot be stopped for good, so the only sequence that works is:
release them and open the device from the same root process, right away. That
is why Realsense capture on macOS needs root, and why the open is retried -- the
first attempt after a release can still lose the race to a respawning daemon.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import threading
import time
from typing import Callable, Optional, TypeVar

IS_MAC = sys.platform == "darwin"

# Every macOS release ships a subset of these; killall ignores the rest.
_CAMERA_DAEMONS = ("VDCAssistant", "UVCAssistant", "appleh13camerad", "AppleCameraAssistant")

_ATTEMPT_TIMEOUT_S = 20.0        # a blocked open never returns on its own
_BACKOFF_S = (0.5, 1.0, 2.0, 0.0)  # one entry per attempt; the last wait is unused

T = TypeVar("T")


class RealsenseOpenError(RuntimeError):
    """The camera could not be opened, with the operator's next step in the text."""


def _sudo_hint() -> str:
    return shlex.join(["sudo", *sys.argv])


def require_root() -> None:
    """Raise unless this process can release the camera daemons. No-op off macOS."""
    if not IS_MAC or os.geteuid() == 0:
        return
    raise RealsenseOpenError(
        "Realsense capture on macOS needs root: the system camera daemons hold "
        "the device and only root can release them.\n"
        f"  Run: {_sudo_hint()}"
    )


def release_camera() -> None:
    """Kill the camera daemons so the next open can claim the device."""
    if not IS_MAC:
        return
    subprocess.run(["/usr/bin/killall", *_CAMERA_DAEMONS],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    time.sleep(0.5)   # the device is not free the instant killall returns


def open_with_retry(open_fn: Callable[[], T], log: Optional[Callable[[str], None]] = None) -> T:
    """Call ``open_fn`` until it succeeds, releasing the camera daemons first.

    Each attempt is bounded. ``pipeline.start()`` can block forever when another
    process holds the device, and a blocked call inside the native library
    cannot be interrupted, so an attempt runs on its own thread and is abandoned
    once the deadline passes. Abandoning is safe here only because attempts are
    few and the process is short-lived.
    """
    require_root()
    release_camera()
    last = ""
    for i, wait in enumerate(_BACKOFF_S, 1):
        box: dict = {}

        def attempt() -> None:
            try:
                box["ok"] = open_fn()
            except BaseException as e:                      # noqa: BLE001 - reported below
                box["err"] = f"{type(e).__name__}: {str(e).strip()[:90]}"

        t = threading.Thread(target=attempt, daemon=True)
        t.start()
        t.join(_ATTEMPT_TIMEOUT_S)
        if "ok" in box:
            return box["ok"]
        last = box.get("err") or f"timed out after {_ATTEMPT_TIMEOUT_S:.0f}s"
        if log:
            log(f"realsense open {i}/{len(_BACKOFF_S)}: {last}")
        if i < len(_BACKOFF_S):
            release_camera()
            time.sleep(wait)
    raise RealsenseOpenError(_failure_text(last))


def _failure_text(last: str) -> str:
    """Explain the failure, and say whether a USB replug is the remaining step."""
    try:
        import pyrealsense2 as rs
        seen = len(rs.context().query_devices())
    except Exception:
        seen = 0
    if seen == 0:
        return (f"No Realsense enumerated (last error: {last}).\n"
                "  Fix: unplug the Realsense USB, wait ~2s, plug it back in, and re-run.")
    return (f"A Realsense is attached but would not open (last error: {last}).\n"
            "  Fix: close anything else using the camera, or unplug and replug its USB.")
