"""Serial transport to the VL53L8CH firmware.

This module owns ALL serial I/O. It enumerates ports, detects the ST-LINK
VCP, gates on detection with one precise abort message, opens the link,
performs the handshake, pushes the derived capture config, drives ranging,
and reads framed CNH packets. Parsing those packets is :mod:`parser`'s job.

Serial protocol (single uppercase commands, newline-terminated; binary
frames stream after ``M``)::

    PC -> MCU                                  MCU -> PC
    -----------------------------------------  ------------------------------
    I\\n            identify / is_alive        STH1 VL53L8CH <maj>.<min> ID=<hex>\\n
    C m sb nb bf f i r ax ay ac ar\\n          OK CFG\\n  |  ERR CFG <reason>\\n
                   configure (ints; the 4
                   trailing ROI ints — agg
                   start_x/start_y/cols/rows —
                   default to the full grid)
    M\\n            start ranging              OK START\\n, then binary frames
    S\\n            stop ranging               OK STOP\\n
"""

from __future__ import annotations

import sys
import time
from typing import Iterator, Optional

import serial
import serial.tools.list_ports as list_ports_mod
from serial.tools.list_ports_common import ListPortInfo

from spad_capture.sensors.st.config import END_BYTE, MAGIC, START_BYTE, ST_LINK_VID, Config

# mode_code -> zone count, for rejecting false syncs before trusting a length.
_MODE_ZONES = {1: 16, 2: 64}


class PortNotFoundError(RuntimeError):
    """No usable serial port could be resolved (detection gate failure)."""


class HandshakeError(RuntimeError):
    """The MCU did not identify itself / ack a command as expected."""


# ---------------------------------------------------------------------------
# Port discovery + ST-LINK detection gate
# ---------------------------------------------------------------------------


def list_ports() -> list[ListPortInfo]:
    """All serial ports via ``serial.tools.list_ports.comports()``."""
    return list(list_ports_mod.comports())


def find_st_link_port() -> Optional[str]:
    """Return the device path of the first ST-LINK VCP, else None.

    Prefers VID 0x0483, falls back to platform-specific path/description patterns.
    """
    ports = list_ports()
    for p in ports:
        if p.vid == ST_LINK_VID:
            return p.device

    def _has_vid(p: ListPortInfo) -> bool:
        return p.vid is not None

    if any(_has_vid(p) for p in ports):
        # Some port exposed a VID but none matched; don't fall back blindly.
        return None

    if sys.platform.startswith("linux"):
        cands = sorted(p.device for p in ports if p.device.startswith("/dev/ttyACM"))
    elif sys.platform == "darwin":
        cands = sorted(p.device for p in ports if "usbmodem" in p.device)
    elif sys.platform == "win32":
        cands = [
            p.device for p in ports
            if any(d in (p.description or "") for d in ("STMicroelectronics", "STLink"))
        ]
    else:
        cands = []
    return cands[0] if cands else None


def _ports_seen_block() -> str:
    """Render the 'ports seen' section of the detection-gate abort message.

    Lists USB ports (those exposing a VID/PID or a real description); the
    legacy ``/dev/ttyS*`` placeholders are omitted to keep the message
    actionable.
    """
    lines = []
    for p in list_ports():
        is_usb = p.vid is not None or (p.description and p.description != "n/a")
        if not is_usb:
            continue
        vidpid = (
            f"{p.vid:04x}:{p.pid:04x}" if (p.vid is not None and p.pid is not None) else "????:????"
        )
        desc = p.description or ""
        mark = "   <-- matches ST-LINK VID" if p.vid == ST_LINK_VID else ""
        lines.append(f"    {p.device:<14} {vidpid}  {desc}{mark}")
    return "\n".join(lines) if lines else "    (none — no USB serial devices detected)"


def require_port(explicit: Optional[str]) -> str:
    """Resolve the port to use, or abort with one precise, actionable message.

    - If ``explicit`` is given and the port exists, return it.
    - Else auto-detect the ST-LINK VCP.
    - On failure raise :class:`PortNotFoundError` listing the ports seen and
      what was expected.
    """
    if explicit:
        if any(p.device == explicit for p in list_ports()):
            return explicit
        raise PortNotFoundError(
            f"Requested port {explicit} not found.\n"
            f"  Ports seen:\n{_ports_seen_block()}\n"
            f"  Fix: pass an existing --port, or plug in the NUCLEO board."
        )

    found = find_st_link_port()
    if found:
        return found

    plat_hint = {
        "linux": "/dev/ttyACM*",
        "darwin": "/dev/cu.usbmodem*",
        "win32": "an STMicroelectronics COM port",
    }.get(
        "linux" if sys.platform.startswith("linux") else sys.platform,
        "/dev/ttyACM* on Linux",
    )
    raise PortNotFoundError(
        "No ST-LINK VCP found.\n"
        f"  Expected: a serial port with USB VID 0x{ST_LINK_VID:04x} (ST-LINK/V2.1),\n"
        f"            typically {plat_hint}.\n"
        f"  Ports seen:\n{_ports_seen_block()}\n"
        "  Fix: plug in the NUCLEO board, or pass --port explicitly."
    )


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


class VL53L8CHTransport:
    """Owns the serial link to the MCU firmware. Use as a context manager::

        with VL53L8CHTransport(cfg) as t:
            t.handshake(); t.configure(cfg); t.start_ranging()
            for raw in t.frames():
                ...
    """

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._serial: Optional[serial.Serial] = None
        self._stop = False
        self._ranging = False
        self._frame_count = 0          # host monotonic frame index (per session)

    def next_index(self) -> int:
        """Next host-assigned monotonic frame index (the MCU counter resets per start)."""
        i = self._frame_count
        self._frame_count += 1
        return i

    # -- context manager ----------------------------------------------------

    def __enter__(self) -> "VL53L8CHTransport":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        try:
            self.stop_ranging()
        except Exception:
            pass
        self.close()

    # -- lifecycle ----------------------------------------------------------

    def open(self) -> None:
        """Resolve the port (detection gate), open it, halt any leftover stream, drain.

        A prior session killed mid-ranging leaves the firmware streaming binary
        frames; send ``S`` and drain until the link is quiet so the handshake
        sees clean ASCII rather than mid-frame binary.
        """
        port = require_port(self.cfg.sensor.port)
        self._serial = serial.Serial(
            port, self.cfg.sensor.baudrate, timeout=self.cfg.sensor.timeout_s
        )
        time.sleep(self.cfg.sensor.init_wait_s)
        self._serial.reset_input_buffer()
        self._serial.write(b"S\n")          # stop any leftover ranging
        self._quiet_drain()

    def _quiet_drain(self, quiet_s: float = 0.2, max_s: float = 2.0) -> None:
        """Read and discard until the stream is silent for ``quiet_s`` (cap ``max_s``)."""
        assert self._serial is not None
        end = time.time() + max_s
        last = time.time()
        while time.time() < end:
            n = self._serial.in_waiting
            if n:
                self._serial.read(n)
                last = time.time()
            elif time.time() - last >= quiet_s:
                return
            else:
                time.sleep(0.02)

    def close(self) -> None:
        """Close the serial port. Idempotent."""
        self._stop = True
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:
                pass
            self._serial = None

    # -- protocol primitives ------------------------------------------------

    def _drain(self) -> None:
        if self._serial and self._serial.in_waiting:
            self._serial.read(self._serial.in_waiting)

    def _write_line(self, text: str) -> None:
        assert self._serial is not None
        self._serial.write(text.encode("ascii"))

    def _read_line(self, *, timeout_s: Optional[float] = None) -> str:
        """Read one newline-terminated ASCII line, skipping any stray binary bytes."""
        assert self._serial is not None
        deadline = time.time() + (timeout_s if timeout_s is not None else self.cfg.sensor.timeout_s)
        buf = bytearray()
        while time.time() < deadline:
            chunk = self._serial.read(1)
            if not chunk:
                continue
            if chunk == b"\n":
                return buf.decode("ascii", errors="replace").rstrip("\r")
            buf += chunk
        return buf.decode("ascii", errors="replace").rstrip("\r")

    def _wait_ack(self, ok: str, label: str, *, timeout_s: float = 3.0) -> str:
        """Read lines until ``ok`` or an ``ERR`` line appears; raise on failure."""
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            line = self._read_line(timeout_s=deadline - time.time())
            if not line:
                continue
            if line.startswith(ok):
                return line
            if line.startswith("ERR"):
                raise HandshakeError(f"{label} rejected: {line.strip()}")
        raise HandshakeError(f"No '{ok}' ack for {label} within {timeout_s}s.")

    # -- handshake ----------------------------------------------------------

    def handshake(self, retries: int = 3) -> dict:
        """Identify the device. Returns ``{fw_major, fw_minor, device_id}``.

        Sends ``I`` and parses ``STH1 VL53L8CH <maj>.<min> ID=<hex>``. Raises
        :class:`HandshakeError` on timeout or a malformed/foreign reply.
        """
        assert self._serial is not None
        last = ""
        for _ in range(max(1, retries)):
            self._drain()
            self._write_line("I\n")
            deadline = time.time() + self.cfg.sensor.timeout_s
            while time.time() < deadline:
                line = self._read_line(timeout_s=deadline - time.time())
                if not line:
                    continue
                last = line
                if line.startswith(MAGIC.decode()) and "ID=" in line:
                    return self._parse_id_line(line)
        raise HandshakeError(
            f"Handshake failed. Expected '{MAGIC.decode()} VL53L8CH <ver> ID=<hex>', "
            f"got: {last!r}"
        )

    @staticmethod
    def _parse_id_line(line: str) -> dict:
        out: dict = {"raw": line}
        toks = line.split()
        # toks: ["STH1", "VL53L8CH", "<maj>.<min>", "ID=<hex>"]
        for tok in toks:
            if tok.startswith("ID="):
                try:
                    out["device_id"] = int(tok.split("=", 1)[1], 16)
                except ValueError:
                    out["device_id"] = None
            elif "." in tok and tok.replace(".", "").isdigit():
                maj, _, minor = tok.partition(".")
                out["fw_major"], out["fw_minor"] = int(maj), int(minor)
        return out

    def configure(self, cfg: Config) -> None:
        """Push the derived integers to the MCU via the ``C`` command.

        Sends ``C mode start_bin num_bins binning_factor freq integ ranging
        agg_start_x agg_start_y agg_cols agg_rows``. Only integers cross the
        wire; mm never does. The 4 trailing ROI ints default to the full grid
        (``0 0 W H``) when no zone/ROI is set, reproducing today's device call.
        """
        r = cfg.resolved()
        mode_code = 1 if r["mode"] == "4x4" else 2
        line = (
            f"C {mode_code} {r['start_bin']} {r['num_bins']} {r['binning_factor']} "
            f"{r['ranging_frequency_hz']} {r['integration_time_ms']} "
            f"{cfg.sensor.ranging_mode.code} "
            f"{r['agg_start_x']} {r['agg_start_y']} {r['agg_cols']} {r['agg_rows']}\n"
        )
        self._drain()
        self._write_line(line)
        self._wait_ack("OK CFG", "configure")

    def start_ranging(self) -> None:
        """Send ``M`` and wait for ``OK START``; binary frames follow."""
        self._drain()
        self._write_line("M\n")
        self._wait_ack("OK START", "start_ranging")
        self._ranging = True
        self._stop = False

    def stop_ranging(self) -> None:
        """Send ``S`` and best-effort wait for ``OK STOP``. Never raises."""
        self._stop = True
        if self._serial is None or not self._ranging:
            return
        try:
            self._write_line("S\n")
            deadline = time.time() + 2.0
            while time.time() < deadline:
                line = self._read_line(timeout_s=deadline - time.time())
                if line.startswith("OK STOP"):
                    break
        except Exception:
            pass
        finally:
            self._ranging = False

    # -- raw frame I/O ------------------------------------------------------

    _SYNC = bytes([START_BYTE]) + MAGIC   # the 5-byte START + MAGIC sequence

    def read_frame(self) -> bytes:
        """Block until one full framed packet (START..END) is read; return it raw.

        Resynchronizes on garbage with a sliding window so a stray ``0xAA``
        cannot swallow the true start byte: it scans for the 5-byte
        ``START_BYTE + MAGIC`` sequence, reads the header, then the declared
        payload + crc + end byte. A corrupt terminator is discarded and the
        search resumes from the next byte (no raise).
        """
        assert self._serial is not None
        ser = self._serial
        sync = self._SYNC
        n_sync = len(sync)
        window = bytearray()
        while not self._stop:
            # 1. slide a 5-byte window until it equals START + MAGIC.
            b = ser.read(1)
            if not b:
                continue
            window += b
            if len(window) > n_sync:
                del window[0]
            if window != sync:
                continue

            # 2. read the rest of the fixed header (after start + magic).
            rest = self._read_exact(15)  # index(4)+dev_ts(4)+mode_code(1)+H(2)+W(2)+B(2)
            if rest is None:
                window.clear()
                continue
            mode_code = rest[8]
            h = int.from_bytes(rest[9:11], "little")
            w = int.from_bytes(rest[11:13], "little")
            nbins = int.from_bytes(rest[13:15], "little")
            # Reject implausible geometry from a false sync before trusting the
            # declared payload length (parse_frame re-validates + CRC downstream).
            if (mode_code not in _MODE_ZONES
                    or not (1 <= h * w <= _MODE_ZONES[mode_code])
                    or not (0 < nbins <= 255)):
                window.clear()
                continue

            # 3. read payload + crc + end byte.
            payload_len = h * w * nbins * 4 + h * w * 4 + 4  # hist + ambient + crc32
            payload = self._read_exact(payload_len + 1)      # + end byte
            window.clear()
            if payload is None:
                continue
            if payload[-1] != END_BYTE:
                continue  # corrupt frame; resync from the next byte

            return bytes(sync) + rest + payload
        return b""

    def _read_exact(self, n: int) -> Optional[bytes]:
        """Read exactly ``n`` bytes (honoring the serial timeout). None on short read."""
        assert self._serial is not None
        buf = bytearray()
        while len(buf) < n and not self._stop:
            chunk = self._serial.read(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return bytes(buf) if len(buf) == n else None
