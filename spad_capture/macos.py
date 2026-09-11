"""Opening the Realsense, and reporting why it will not open.

The retry and the diagnosis run on every platform. Only the daemon handling
below is macOS-specific, and it no-ops elsewhere.

macOS routes UVC devices through camera assistant daemons that claim the
Realsense the moment anything enumerates it. They run as a system user and
respawn on demand, so the device has to be released and opened from the same
root process. Measured on this platform, an ordinary user cannot open the camera
even with nothing else using it.

Releasing the daemons does not settle the race. A respawning daemon can still
win the next attempt, so the open is retried.
"""

from __future__ import annotations

import glob
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
# One wait per attempt; the last is unused. Few attempts, long waits: a camera
# another process just released needs seconds, and each attempt disturbs it.
# Measured: an immediate retry storm fails where a 15 s pause succeeds.
_BACKOFF_S = (2.0, 5.0, 10.0, 0.0)
_UNPRIVILEGED_ATTEMPTS = 1       # then report root as the next step
_PIP_INSTALL = "pip install -e '.[rgb]'"
_INSTALL_CMD = "conda install -c conda-forge pyrealsense2" if IS_MAC else _PIP_INSTALL

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

    Without the privilege to release the daemons, one attempt is made and the
    failure names ``sudo`` as the next step.

    Each attempt is bounded. ``pipeline.start()`` can block forever when another
    process holds the device, and the native call cannot be interrupted, so an
    attempt runs on its own thread and is abandoned at the deadline. Attempts are
    few, which bounds the abandoned threads.
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
        if log and len(waits) > 1:      # a lone attempt reports through the error
            log(f"realsense open {i}/{len(waits)}: {last}")
        if i < len(waits):
            if can_release:
                release_camera()
            time.sleep(wait)
    raise RealsenseOpenError(_failure_text(last))


_STUCK = "failed to set power state"


def binding_source() -> str:
    """Where ``pyrealsense2`` came from: ``pip``, ``conda`` or ``unknown``.

    Which build is installed decides whether the camera works at all, and the
    right build differs by platform: PyPI publishes no macOS wheel, and the
    conda-forge build cannot reach the camera on Linux.
    """
    try:
        import importlib.metadata as md
        md.version("pyrealsense2")      # conda-forge ships no dist-info
        return "pip"
    except Exception:
        pass
    # sys.prefix, not CONDA_PREFIX: the env var is unset when the env's python
    # is invoked by path rather than activated.
    if glob.glob(os.path.join(sys.prefix, "conda-meta", "pyrealsense2-*.json")):
        return "conda"
    return "unknown"


def _classify(err: str) -> tuple[str, str]:
    """Name the cause behind a refused camera access."""
    if _STUCK not in err:
        return "error", err
    if IS_MAC and os.geteuid() != 0:
        return "hidden", "an unprivileged process cannot reach the camera on macOS"
    if not IS_MAC and binding_source() == "conda":
        return "wrong-build", "the conda-forge binding cannot reach the camera on Linux"
    return "stuck", err


def camera_state() -> tuple[str, str]:
    """Classify the camera as ``ok``, ``absent``, ``stuck``, ``hidden`` or ``error``.

    ``stuck`` is a camera that enumerates but refuses every access. No amount of
    retrying clears it; the USB device has to be reset.
    """
    try:
        import pyrealsense2 as rs
    except Exception:
        return "missing", "pyrealsense2 is not installed"
    try:
        devices = list(rs.context().query_devices())
    except Exception as e:
        return _classify(str(e).strip())
    if not devices:
        if IS_MAC and os.geteuid() != 0:
            return "hidden", "an unprivileged process sees no camera on macOS"
        return "absent", "no camera on the USB bus"
    try:
        named = [f"{d.get_info(rs.camera_info.name)} "
                 f"{d.get_info(rs.camera_info.serial_number)}" for d in devices]
    except Exception as e:
        return _classify(str(e).strip())
    return "ok", " · ".join(named)


def fix_for(state: str) -> tuple[str, Optional[str]]:
    """What clears ``state``, and the command that does it."""
    if state == "absent":
        return "plug the camera in, or unplug and replug its USB", None
    if state == "stuck":
        if IS_MAC:
            return "unplug the USB, wait ~2s, plug it back in", None
        return "reset its USB, or unplug and replug", "sudo usbreset <id from lsusb>"
    if state == "hidden":
        return "re-run under sudo", _sudo_hint()
    if state == "missing":
        return "install the Realsense binding", _INSTALL_CMD
    if state == "wrong-build":
        return "replace it with the PyPI wheel", _PIP_INSTALL
    return "check the camera and its cable", None


def _failure_text(last: str) -> str:
    """Explain the failure and name the one action that clears it."""
    state, detail = camera_state()
    if state == "ok":
        return (f"Realsense would not open ({last}).\n"
                "  Fix: re-run; a camera another process just released needs a "
                "few seconds.")
    advice, cmd = fix_for(state)
    return (f"Realsense {state} ({detail}).\n"
            f"  Fix: {advice}" + (f":\n       {cmd}" if cmd else "."))
