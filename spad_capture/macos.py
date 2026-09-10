"""Opening the Realsense on macOS. Every entry point no-ops off macOS.

macOS routes UVC devices through camera assistant daemons that claim the
Realsense the moment anything enumerates it. They run as a system user and
respawn on demand, so they cannot be stopped for good: the device has to be
released and opened from the same root process, right away. Measured here, an
ordinary user cannot open the camera even with nothing else using it, so on
macOS this is a hard requirement rather than a fallback.

The open is retried because releasing the daemons does not settle the race --
a respawning daemon can still win the next attempt.
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
_BACKOFF_S = (0.5, 1.0, 2.0, 2.0, 2.0, 0.0)  # one per attempt; the last wait is unused
_UNPRIVILEGED_ATTEMPTS = 1       # then say plainly that root is the next step

T = TypeVar("T")


class RealsenseOpenError(RuntimeError):
    """The camera could not be opened, with the operator's next step in the text."""


def _sudo_hint() -> str:
    return shlex.join(["sudo", *sys.argv])


def release_camera() -> None:
    """Kill the camera daemons so the next open can claim the device. Needs root."""
    if not IS_MAC:
        return
    subprocess.run(["/usr/bin/killall", *_CAMERA_DAEMONS],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    time.sleep(0.5)   # the device is not free the instant killall returns


def _run_attempt(open_fn: Callable[[], T], box: dict) -> None:
    try:
        box["ok"] = open_fn()
    except BaseException as e:                          # noqa: BLE001 - reported by the caller
        box["err"] = f"{type(e).__name__}: {str(e).strip()[:90]}"


def open_with_retry(open_fn: Callable[[], T], log: Optional[Callable[[str], None]] = None) -> T:
    """Call ``open_fn`` until it succeeds, releasing the macOS camera daemons between tries.

    Root is not demanded up front: when nothing else holds the camera an ordinary
    user opens it fine, and asking for more privilege than the job needs is worse
    than trying. Only once an unprivileged open has actually failed on macOS does
    this report that releasing the daemons -- which needs root -- is the next step.

    Each attempt is bounded. ``pipeline.start()`` can block forever when another
    process holds the device, and a blocked call inside the native library cannot
    be interrupted, so an attempt runs on its own thread and is abandoned once the
    deadline passes. Abandoning is safe here only because attempts are few.
    """
    can_release = not IS_MAC or os.geteuid() == 0
    if can_release:
        release_camera()
    waits = _BACKOFF_S if can_release else _BACKOFF_S[:_UNPRIVILEGED_ATTEMPTS]
    last = ""
    for i, wait in enumerate(waits, 1):
        box: dict = {}
        t = threading.Thread(target=_run_attempt, args=(open_fn, box), daemon=True)
        t.start()
        t.join(_ATTEMPT_TIMEOUT_S)
        if "ok" in box:
            return box["ok"]
        last = box.get("err") or f"timed out after {_ATTEMPT_TIMEOUT_S:.0f}s"
        if log:
            log(f"realsense open {i}/{len(waits)}: {last}")
        if i < len(waits):
            if can_release:
                release_camera()
            time.sleep(wait)
    if not can_release:
        raise RealsenseOpenError(
            f"Could not open the Realsense as this user (last error: {last}).\n"
            "  Something else holds the camera. On macOS that is usually the system\n"
            "  camera daemons, and only root can release them.\n"
            f"  Run: {_sudo_hint()}"
        )
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
                "  The camera can drop off the USB bus outright, and nothing in software\n"
                "  brings it back.\n"
                "  Fix: unplug the Realsense USB, wait ~2s, plug it back in, and re-run.")
    return (f"A Realsense is attached but would not open (last error: {last}).\n"
            "  Fix: close anything else using the camera, or unplug and replug its USB.")
