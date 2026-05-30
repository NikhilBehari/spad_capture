"""Frame writers. Select via config.storage.format: pkl | npy | h5 | none."""

from __future__ import annotations

import json
import pickle
from abc import ABC, abstractmethod
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version as _pkg_version
from pathlib import Path
from typing import Any, Optional

import numpy as np
import yaml

from spad_capture.config import Config, StorageFormat
from spad_capture.sensor import Frame


def _spad_capture_version() -> str:
    try:
        return _pkg_version("spad_capture")
    except PackageNotFoundError:
        return "unknown"


def _metadata_to_json(meta: dict) -> str:
    """Serialize ``meta`` as pretty-printed JSON, but keep small integer
    pairs (SPAD coords, output_pos, output_shape) on a single line for
    readability."""
    import re
    raw = json.dumps(meta, indent=2, default=str)
    return re.sub(r"\[\s*(-?\d+),\s*(-?\d+)\s*\]", r"[\1, \2]", raw)


def _render_run_dir(template: str, cfg: Config) -> str:
    import re
    # zone_mode is "3x3_wide", "4x4_narrow_v3", ...; underscores collide with
    # the folder-name separator, so strip them: "3x3wide", "4x4narrowv3".
    zone_compact = cfg.sensor.zone_mode.value.replace("_", "")
    # range_mode is "long" / "short" on its own; appending "range" makes the
    # folder name self-documenting: "longrange" / "shortrange".
    range_compact = f"{cfg.sensor.range_mode.value}range"
    rendered = template.format(
        timestamp=datetime.now().strftime("%Y%m%d"),
        zone_mode=zone_compact,
        range_mode=range_compact,
        capture_mode=cfg.capture.mode.value,
        name=cfg.name or "",
    )
    # Collapse consecutive separators left by an empty {name} placeholder.
    rendered = re.sub(r"_+", "_", rendered).rstrip("_")
    return rendered


def _resolve_run_dir(root: Path, name: str) -> Path:
    """Return ``root / name``, appending ``_2``, ``_3``, ... when the
    folder already exists. This prevents same-day runs of identical
    configs from silently overwriting each other."""
    candidate = root / name
    if not candidate.exists():
        return candidate
    n = 2
    while (root / f"{name}_{n}").exists():
        n += 1
    return root / f"{name}_{n}"


def _frame_to_record(f: Frame) -> dict[str, Any]:
    rec: dict[str, Any] = {"index": f.index, "timestamp": f.timestamp, "histogram": f.histogram}
    if f.rgb_bgr is not None:
        rec["rgb_bgr"] = f.rgb_bgr
    if f.depth_mm is not None:
        rec["depth_mm"] = f.depth_mm
    if f.rgb_intrinsics is not None:
        rec["rgb_intrinsics"] = f.rgb_intrinsics
    return rec


def _build_metadata(cfg: Config, *, calibration: Optional[dict] = None) -> dict[str, Any]:
    meta = {
        "name": cfg.name,
        "sensor": cfg.sensor.model_dump(mode="json"),
        "firmware": cfg.firmware.model_dump(mode="json"),
        "capture": cfg.capture.model_dump(mode="json"),
        "storage": cfg.storage.model_dump(mode="json"),
        "viz": cfg.viz.model_dump(mode="json"),
        "rgb": cfg.rgb.model_dump(mode="json"),
        "mask": cfg.mask,
        "calibration": calibration if calibration is not None else {
            "requested": cfg.sensor.calibrate,
            "run": False,
        },
        "created_at": datetime.now().isoformat(),
        "version": _spad_capture_version(),
    }
    # ``resolved_zones`` (the canonical layout: one entry per output
    # channel, in stream order) is populated later by the sensor through
    # ``update_layout``. The format is identical for predefined and custom
    # modes, so downstream consumers do not have to special-case either.
    return meta


# ---------------------------------------------------------------------------
# Base interface
# ---------------------------------------------------------------------------


class Writer(ABC):
    """Append-style frame writer. Creates a per-run subfolder under storage.root."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        run_name = _render_run_dir(cfg.storage.run_dir_template, cfg)
        self.run_dir = _resolve_run_dir(cfg.storage.root, run_name).expanduser().resolve()
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.metadata = _build_metadata(cfg)
        if cfg.storage.save_metadata:
            (self.run_dir / "metadata.json").write_text(_metadata_to_json(self.metadata))
        if cfg.storage.save_resolved_config:
            (self.run_dir / "config.yaml").write_text(
                yaml.safe_dump(cfg.model_dump(mode="json"), sort_keys=False)
            )

    def update_calibration(self, calibration: dict) -> None:
        """Refresh metadata.json with the actual cal outcome (status + bytes)."""
        self.metadata["calibration"] = calibration
        self._rewrite_metadata()

    def update_layout(self, layout_info: dict) -> None:
        """Stamp the sensor's full spatial-layout descriptor into metadata.

        Captures FoV, sub-capture structure, bin width, and (for 8x8) the
        non-trivial channel remap, so every saved file is self-describing
        without reference to library source.
        """
        self.metadata["sensor_layout"] = layout_info
        self._rewrite_metadata()

    def _rewrite_metadata(self) -> None:
        if self.cfg.storage.save_metadata:
            (self.run_dir / "metadata.json").write_text(_metadata_to_json(self.metadata))

    @property
    @abstractmethod
    def path(self) -> Path: ...

    @abstractmethod
    def write(self, frame: Frame) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    def __enter__(self) -> "Writer":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Implementations
# ---------------------------------------------------------------------------


class PklWriter(Writer):
    """Append-only pickle stream; one dict per frame, metadata as first record."""

    def __init__(self, cfg: Config):
        super().__init__(cfg)
        self._path = self.run_dir / f"{cfg.storage.data_filename}.pkl"
        self._fh = open(self._path, "wb")
        pickle.dump({"_metadata": self.metadata}, self._fh)

    @property
    def path(self) -> Path:
        return self._path

    def write(self, frame: Frame) -> None:
        pickle.dump(_frame_to_record(frame), self._fh)

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.flush()
            self._fh.close()


class NpyWriter(Writer):
    """In-memory buffer flushed to a single (N, H, W, B) .npy at close."""

    def __init__(self, cfg: Config):
        super().__init__(cfg)
        self._path = self.run_dir / f"{cfg.storage.data_filename}.npy"
        self._buf: list[Frame] = []

    @property
    def path(self) -> Path:
        return self._path

    def write(self, frame: Frame) -> None:
        self._buf.append(frame)

    def close(self) -> None:
        if not self._buf:
            return
        hist_stack = np.stack([f.histogram for f in self._buf])
        np.save(self._path, hist_stack)
        # Companion file with timestamps so you can reconstruct timing.
        np.save(
            self._path.with_name(self._path.stem + "_ts.npy"),
            np.array([(f.index, f.timestamp) for f in self._buf]),
        )
        self._buf.clear()


class HdfWriter(Writer):
    """Streamed HDF5 with extendable /frames, /timestamps, /metadata, optional /rgb + /depth."""

    def __init__(self, cfg: Config):
        super().__init__(cfg)
        import h5py
        self._h5py = h5py
        self._path = self.run_dir / f"{cfg.storage.data_filename}.h5"
        self._f = h5py.File(self._path, "w")
        self._f.create_group("metadata").attrs["json"] = json.dumps(self.metadata, default=str)
        self._frames = None
        self._rgb = None
        self._depth = None

    def _rewrite_metadata(self) -> None:
        super()._rewrite_metadata()
        # Keep the in-file metadata copy in sync with the sidecar JSON.
        if self._f is not None and "metadata" in self._f:
            self._f["metadata"].attrs["json"] = json.dumps(self.metadata, default=str)

    @property
    def path(self) -> Path:
        return self._path

    def _ensure_datasets(self, frame: Frame) -> None:
        if self._frames is None:
            h = frame.histogram
            self._frames = self._f.create_dataset(
                "frames", shape=(0, *h.shape), maxshape=(None, *h.shape),
                dtype=h.dtype, chunks=True, compression="gzip",
            )
            self._ts = self._f.create_dataset(
                "timestamps", shape=(0,), maxshape=(None,), dtype="f8", chunks=True,
            )
            self._idx = self._f.create_dataset(
                "frame_index", shape=(0,), maxshape=(None,), dtype="i8", chunks=True,
            )
        if frame.rgb_bgr is not None and self._rgb is None:
            rh, rw, rc = frame.rgb_bgr.shape
            self._rgb = self._f.create_dataset(
                "rgb_bgr", shape=(0, rh, rw, rc), maxshape=(None, rh, rw, rc),
                dtype="u1", chunks=(1, rh, rw, rc), compression="gzip",
            )
            if frame.rgb_intrinsics:
                self._f.create_group("rgb_intrinsics").attrs["json"] = json.dumps(frame.rgb_intrinsics)
        if frame.depth_mm is not None and self._depth is None:
            dh, dw = frame.depth_mm.shape
            self._depth = self._f.create_dataset(
                "depth_mm", shape=(0, dh, dw), maxshape=(None, dh, dw),
                dtype="u2", chunks=(1, dh, dw), compression="gzip",
            )

    def write(self, frame: Frame) -> None:
        self._ensure_datasets(frame)
        n = self._frames.shape[0]
        self._frames.resize(n + 1, axis=0); self._frames[n] = frame.histogram
        self._ts.resize(n + 1, axis=0);     self._ts[n] = frame.timestamp
        self._idx.resize(n + 1, axis=0);    self._idx[n] = frame.index
        if self._rgb is not None and frame.rgb_bgr is not None:
            self._rgb.resize(n + 1, axis=0); self._rgb[n] = frame.rgb_bgr
        if self._depth is not None and frame.depth_mm is not None:
            self._depth.resize(n + 1, axis=0); self._depth[n] = frame.depth_mm

    def close(self) -> None:
        if self._f:
            self._f.close()
            self._f = None


class NullWriter(Writer):
    """No-op writer; still creates the run dir + sidecars but writes no data."""

    @property
    def path(self) -> Path:
        return self.run_dir

    def write(self, frame: Frame) -> None:
        pass

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


_WRITERS: dict[StorageFormat, type[Writer]] = {
    StorageFormat.PKL: PklWriter,
    StorageFormat.NPY: NpyWriter,
    StorageFormat.HDF5: HdfWriter,
    StorageFormat.NONE: NullWriter,
}


def make_writer(cfg: Config) -> Writer:
    """Build the writer instance from a config."""
    cls = _WRITERS[cfg.storage.format]
    return cls(cfg)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def load(path: Path | str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Load any of the supported formats; returns (metadata, frames_list).

    Each frame is a dict with keys: ``index``, ``timestamp``, ``histogram``.

    Always prefers the sidecar ``metadata.json`` in the same directory over
    any copy embedded in the data file: the sidecar is rewritten as the
    sensor finishes configuring (cal status, layout etc.), whereas pickle
    and HDF embeds are written at run-start before those fields exist.
    """
    p = Path(path)
    ext = p.suffix.lower().lstrip(".")
    sidecar = p.parent / "metadata.json"
    sidecar_meta: Optional[dict[str, Any]] = None
    if sidecar.exists():
        try:
            sidecar_meta = json.loads(sidecar.read_text())
        except Exception:
            sidecar_meta = None
    def _prefer_sidecar(embedded: dict[str, Any]) -> dict[str, Any]:
        return sidecar_meta if sidecar_meta is not None else embedded
    if ext == "pkl":
        metadata: dict[str, Any] = {}
        frames: list[dict[str, Any]] = []
        with open(p, "rb") as f:
            while True:
                try:
                    rec = pickle.load(f)
                except EOFError:
                    break
                if "_metadata" in rec:
                    metadata = rec["_metadata"]
                else:
                    frames.append(rec)
        return _prefer_sidecar(metadata), frames
    if ext == "npy":
        # Redirect from companion timestamp file to the main stack if needed.
        if p.stem.endswith("_ts"):
            p = p.with_name(p.stem[: -len("_ts")] + ".npy")
        hists = np.load(p)
        ts_path = p.with_name(p.stem + "_ts.npy")
        ts = np.load(ts_path) if ts_path.exists() else None
        # Per-run metadata lives next to the data file inside the run dir.
        meta_path = p.parent / "metadata.json"
        metadata = json.loads(meta_path.read_text()) if meta_path.exists() else {}
        frames = [
            {"index": int(ts[i, 0]) if ts is not None else i,
             "timestamp": float(ts[i, 1]) if ts is not None else 0.0,
             "histogram": hists[i]}
            for i in range(hists.shape[0])
        ]
        return _prefer_sidecar(metadata), frames
    if ext == "h5":
        import h5py
        with h5py.File(p, "r") as f:
            metadata = json.loads(f["metadata"].attrs["json"])
            hists = f["frames"][:]
            ts = f["timestamps"][:]
            idx = f["frame_index"][:]
            rgb = f["rgb_bgr"][:] if "rgb_bgr" in f else None
            depth = f["depth_mm"][:] if "depth_mm" in f else None
        frames = []
        for i in range(hists.shape[0]):
            r = {"index": int(idx[i]), "timestamp": float(ts[i]), "histogram": hists[i]}
            if rgb is not None:
                r["rgb_bgr"] = rgb[i]
            if depth is not None:
                r["depth_mm"] = depth[i]
            frames.append(r)
        return _prefer_sidecar(metadata), frames
    raise ValueError(f"Unsupported file format: {ext}")


def user_zone_histograms(metadata: dict[str, Any], frames: list[dict[str, Any]]):
    """Return ``(zone_ids, hists)`` containing only the user's intended zones.

    For predefined zone modes the call is a pass-through: every output
    cell is a user zone, so ``zone_ids`` is ``None`` and ``hists`` is the
    full per-frame histogram array unchanged.

    For custom-mask captures that include auto-added dummy pixels (when
    fewer than 4 user zones were supplied), the dummy entries are stripped.
    ``zone_ids[i]`` is the user-supplied zone id for column ``i`` of
    ``hists``. ``hists`` has shape ``(num_frames, num_user_zones,
    num_bins)`` in ascending user-zone-id order.

    Usage::

        from spad_capture.storage import load, user_zone_histograms
        meta, frames = load("outputs/<run>/data.h5")
        zone_ids, hists = user_zone_histograms(meta, frames)
        # hists[:, i, :] is the histogram for user zone zone_ids[i]
    """
    import numpy as np
    # Canonical location: metadata.sensor_layout.resolved_zones. Older
    # captures stored it at metadata.resolved_zones; both are accepted.
    resolved = (metadata.get("sensor_layout") or {}).get("resolved_zones")
    if not resolved:
        resolved = metadata.get("resolved_zones")
    if not resolved or not any(rz.get("is_dummy") for rz in resolved):
        # No dummies to strip; every cell is user-intended.
        hists = np.stack([np.asarray(f["histogram"]) for f in frames])
        return None, hists
    user_indices = [rz["output_index"] for rz in resolved if not rz["is_dummy"]]
    # Accept both "zone_id" (current) and "user_zone_id" (legacy) keys.
    zone_ids = [rz.get("zone_id", rz.get("user_zone_id")) for rz in resolved if not rz["is_dummy"]]
    out = []
    for f in frames:
        h = np.asarray(f["histogram"])
        # Histogram shape from custom captures is (1, total_zones, num_bins);
        # reshape to (total_zones, num_bins) before picking the user ones.
        flat = h.reshape(-1, h.shape[-1])
        out.append(flat[user_indices])
    return zone_ids, np.stack(out)
