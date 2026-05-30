"""FastAPI + WebSocket server pushing histograms to a bundled static page.

Wire format (binary; sent via send_bytes):

    offset  type        field
    ------- ----------- ------------------------------
    0       u32 LE      frame index
    4       u32 LE      height (zone rows)
    8       u32 LE      width  (zone cols)
    12      u32 LE      num_bins (per channel)
    16      f64 LE      timestamp (unix seconds)
    24      u32 LE      rgb_jpeg size in bytes (0 if no RGB)
    28      u32 LE      depth_jpeg size in bytes (0 if no depth)
    32+     i32 LE *N   histograms, row-major (H * W * num_bins values)
    +rgb    u8 *R       JPEG-encoded RGB image (R bytes)
    +depth  u8 *D       JPEG-encoded colorized depth (D bytes)

``VizServer.publish(frame)`` is the only hook the capture loop calls.
Swap ``static/index.html`` to replace the dashboard.
"""

from __future__ import annotations

import asyncio
import json
import queue as _queue
import shutil
import socket
import struct
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

import numpy as np
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from spad_capture.config import Config, VizConfig
from spad_capture.sensor import Frame


STATIC_DIR = Path(__file__).parent / "static"
_HEADER = struct.Struct("<IIIIdII")   # idx, H, W, B, timestamp, rgb_size, depth_size


_cv2 = None
_cv2_warned = False


def _get_cv2():
    """Lazy-import opencv. Warns once if missing so silent encoding failures are visible."""
    global _cv2, _cv2_warned
    if _cv2 is not None:
        return _cv2
    try:
        import cv2 as _c
        _cv2 = _c
        return _cv2
    except ImportError:
        if not _cv2_warned:
            import warnings
            warnings.warn(
                "opencv-python(-headless) not installed; RGB/depth viz encoding is disabled. "
                "Install with: pip install opencv-python-headless",
                stacklevel=2,
            )
            _cv2_warned = True
        return None


def _encode_jpeg(bgr, quality: int = 80) -> bytes:
    """JPEG-encode a BGR uint8 image, returning bytes (empty if cv2 unavailable)."""
    cv2 = _get_cv2()
    if cv2 is None:
        return b""
    ok, buf = cv2.imencode(".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
    return buf.tobytes() if ok else b""


def _encode_depth(depth_mm, quality: int = 75) -> bytes:
    """Normalize 16-bit depth (mm) to a JET-colormap JPEG for viz."""
    cv2 = _get_cv2()
    if cv2 is None:
        return b""
    d = depth_mm.astype(np.float32)
    valid = d > 0
    if not valid.any():
        return b""
    lo, hi = float(d[valid].min()), float(d.max())
    if hi <= lo:
        return b""
    norm = np.clip((d - lo) / (hi - lo), 0.0, 1.0)
    u8 = (norm * 255).astype(np.uint8)
    u8[~valid] = 0
    colored = cv2.applyColorMap(u8, cv2.COLORMAP_JET)
    ok, buf = cv2.imencode(".jpg", colored, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
    return buf.tobytes() if ok else b""


def _outbound_ip() -> Optional[str]:
    """Best-effort: pick the local IPv4 used to reach an external host."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("1.1.1.1", 53))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


def _overlay_ip() -> Optional[str]:
    """If a tailscale-style overlay-network CLI is installed, get its assigned IPv4."""
    cli = shutil.which("tailscale")
    if not cli:
        return None
    try:
        out = subprocess.run([cli, "ip", "-4"], capture_output=True, text=True, timeout=1.0)
        ip = out.stdout.strip().splitlines()[0] if out.stdout.strip() else None
        return ip if ip and ip.count(".") == 3 else None
    except Exception:
        return None


def viz_urls(cfg: VizConfig) -> list[str]:
    """Return every URL the viz is reachable at from this host."""
    proto = "http"
    if cfg.host not in ("0.0.0.0", "::"):
        return [f"{proto}://{cfg.host}:{cfg.port}"]
    urls = [f"{proto}://127.0.0.1:{cfg.port}"]
    seen = {urls[0]}
    for ip in (_outbound_ip(), _overlay_ip()):
        if ip:
            u = f"{proto}://{ip}:{cfg.port}"
            if u not in seen:
                urls.append(u)
                seen.add(u)
    return urls


def _frame_to_bytes(frame: Frame, jpeg_quality: int = 80) -> bytes:
    h, w, b = frame.histogram.shape
    hist = np.ascontiguousarray(frame.histogram, dtype=np.int32)
    rgb_bytes = _encode_jpeg(frame.rgb_bgr, quality=jpeg_quality) if frame.rgb_bgr is not None else b""
    depth_bytes = _encode_depth(frame.depth_mm) if frame.depth_mm is not None else b""
    header = _HEADER.pack(
        int(frame.index), h, w, b, float(frame.timestamp),
        len(rgb_bytes), len(depth_bytes),
    )
    return header + hist.tobytes(order="C") + rgb_bytes + depth_bytes


class VizServer:
    """Background FastAPI + WebSocket server with a ``publish`` hook."""

    def __init__(self, cfg: VizConfig, sensor_meta: Optional[dict] = None, *, jpeg_quality: int = 80):
        self.cfg = cfg
        self.sensor_meta = sensor_meta or {}
        self.jpeg_quality = jpeg_quality
        self._app = FastAPI(title="spad_capture viz")
        self._latest_bytes: Optional[bytes] = None
        self._lock = threading.Lock()
        self._clients: set[WebSocket] = set()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._server: Optional[uvicorn.Server] = None
        # If set, the server is in replay mode and the controller drives publish().
        self.replay: Optional["ReplayController"] = None
        # Pulled by the manual-mode capture loop. Each item triggers one
        # burst (cfg.capture.num_frames saved). WS clients post a
        # {"type": "capture"} text frame to enqueue a trigger.
        self.capture_triggers: _queue.Queue = _queue.Queue()
        self._build_routes()

    def _build_routes(self) -> None:
        app = self._app
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

        @app.get("/", response_class=HTMLResponse)
        async def index() -> str:
            return (STATIC_DIR / "index.html").read_text()

        @app.get("/meta")
        async def meta() -> dict:
            m = dict(self.sensor_meta)
            if self.replay is not None:
                m["mode"] = "replay"
                m.update(self.replay.snapshot())
            else:
                m["mode"] = "live"
            return m

        @app.websocket("/ws")
        async def ws_endpoint(ws: WebSocket) -> None:
            await ws.accept()
            self._clients.add(ws)
            with self._lock:
                latest = self._latest_bytes
            if latest:
                try:
                    await ws.send_bytes(latest)
                except Exception:
                    pass
            try:
                while True:
                    msg = await ws.receive_text()
                    if not msg:
                        continue
                    # Try a JSON envelope first ({"type": "capture", ...}).
                    parsed: Optional[dict] = None
                    try:
                        parsed = json.loads(msg)
                    except Exception:
                        parsed = None
                    msg_type = (parsed or {}).get("type") if isinstance(parsed, dict) else None
                    if msg_type == "capture":
                        # Live manual-mode trigger. Enqueue one burst.
                        self.capture_triggers.put_nowait(time.time())
                        continue
                    # Replay control commands fall through as plain text.
                    if self.replay is not None:
                        self.replay.command(msg)
            except WebSocketDisconnect:
                self._clients.discard(ws)
            except Exception:
                self._clients.discard(ws)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def publish(self, frame: Frame) -> None:
        """Push a frame to all connected clients. Safe to call from any thread."""
        payload = _frame_to_bytes(frame, jpeg_quality=self.jpeg_quality)
        with self._lock:
            self._latest_bytes = payload
        if not self._loop or not self._clients:
            return
        async def _broadcast(p: bytes) -> None:
            dead = []
            for ws in list(self._clients):
                try:
                    await ws.send_bytes(p)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                self._clients.discard(ws)
        try:
            asyncio.run_coroutine_threadsafe(_broadcast(payload), self._loop)
        except RuntimeError:
            pass

    def start(self) -> None:
        """Start uvicorn in a background thread."""
        config = uvicorn.Config(
            self._app,
            host=self.cfg.host,
            port=self.cfg.port,
            log_level="warning",
            access_log=False,
        )
        self._server = uvicorn.Server(config)
        def _run() -> None:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(self._server.serve())
        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()
        # Wait briefly for the server to be up.
        for _ in range(50):
            if self._server.started:
                break
            time.sleep(0.05)

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=2.0)


class ReplayController:
    """Drives a VizServer in replay mode (a finite frame list with playback controls).

    Thread-safe; commands accepted: ``play``, ``pause``, ``next``, ``prev``,
    ``seek <N>``, ``rate <hz>``.
    """

    def __init__(self, server: "VizServer", frames: list[Frame], rate_hz: float = 2.0):
        self.server = server
        self.frames = frames
        self.index = 0
        # Always start paused. Playback is user-driven via prev/next/seek;
        # autoplay would contend with the slider thread and produce choppy
        # output at low replay rates.
        self.paused = True
        self.rate_hz = max(rate_hz, 0.1)
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "total_frames": len(self.frames),
                "current_index": self.index,
                "paused": self.paused,
                "rate_hz": self.rate_hz,
            }

    def command(self, cmd: str) -> None:
        parts = cmd.strip().split()
        if not parts:
            return
        op = parts[0].lower()
        with self._lock:
            if op == "play":
                self.paused = False
            elif op == "pause":
                self.paused = True
            elif op == "next":
                self.paused = True
                self.index = (self.index + 1) % len(self.frames)
                self._publish_locked()
            elif op == "prev":
                self.paused = True
                self.index = (self.index - 1) % len(self.frames)
                self._publish_locked()
            elif op == "seek" and len(parts) >= 2:
                try:
                    n = int(parts[1])
                except ValueError:
                    return
                self.paused = True
                self.index = max(0, min(len(self.frames) - 1, n))
                self._publish_locked()
            elif op == "rate" and len(parts) >= 2:
                try:
                    self.rate_hz = max(0.1, float(parts[1]))
                except ValueError:
                    pass
            self._cv.notify_all()

    def _publish_locked(self) -> None:
        # Caller holds self._lock; release briefly to call publish (which uses its own lock).
        idx = self.index
        frames = self.frames
        self._lock.release()
        try:
            self.server.publish(frames[idx])
        finally:
            self._lock.acquire()

    def _run(self) -> None:
        while not self._stop.is_set():
            with self._cv:
                while self.paused and not self._stop.is_set():
                    self._cv.wait(timeout=0.5)
                if self._stop.is_set():
                    return
                idx = self.index
                self.index = (self.index + 1) % len(self.frames)
                rate = self.rate_hz
            self.server.publish(self.frames[idx])
            time.sleep(1.0 / max(rate, 0.1))

    def start(self) -> None:
        # Publish frame 0 up front so the user sees something immediately
        # even though playback starts paused.
        if self.frames:
            self.server.publish(self.frames[0])
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._cv:
            self._cv.notify_all()
        if self._thread:
            self._thread.join(timeout=2.0)


def run_with_viz(cfg: Config, capture_fn) -> int:
    """Start a VizServer, run capture_fn(cfg, frame_callback=...), stop server."""
    sensor_meta = {
        "zone_mode": cfg.sensor.zone_mode.value,
        "range_mode": cfg.sensor.range_mode.value,
        "capture_mode": cfg.capture.mode.value,
        "rgb_enabled": cfg.rgb.enabled,
        "depth_enabled": cfg.rgb.enabled and cfg.rgb.save_depth,
    }
    # Zone meta (mask_zones / output_labels / resolved_zones) is derived
    # deterministically from zone_mode and mask: predefined modes from the
    # documented SPAD area, custom from the mask resolver. The live viz
    # renders labels and the mask modal immediately without waiting on the
    # sensor to finish configuring.
    from spad_capture.predefined_layouts import build_zone_meta as _build_zm
    sensor_meta.update(_build_zm(cfg.sensor.zone_mode.value, cfg.mask))
    server = VizServer(
        cfg.viz,
        sensor_meta=sensor_meta,
        jpeg_quality=cfg.rgb.jpeg_quality,
    )
    server.start()
    urls = viz_urls(cfg.viz)
    # Print the viz URL up front so the page can be opened while the sensor
    # is still initializing (it shows a "waiting for frames" view until capture starts).
    from rich.console import Console
    from rich.panel import Panel
    Console().print(Panel(
        "\n".join(f"[link={u}]{u}[/link]" for u in urls),
        title="[bold]live viz[/bold]", title_align="left",
        border_style="cyan", padding=(0, 1),
    ))
    try:
        return capture_fn(
            cfg,
            frame_callback=server.publish,
            viz_urls=urls,
            trigger_queue=server.capture_triggers,
        )
    finally:
        server.stop()
