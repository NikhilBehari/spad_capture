"""Frame writers. Select via config.storage.format: pkl | npy | h5 | npz | none.

A backend config needs only ``name``, ``storage`` and ``template_vars()``.
Backends add formats with ``register_writer``.
"""

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

from pydantic import BaseModel

from spad_capture.frame import Frame


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


def _render_run_dir(template: str, cfg: Any) -> str:
    """Render the run-dir name. The sensor supplies its own template values."""
    import re
    rendered = template.format(
        timestamp=datetime.now().strftime("%Y%m%d"),
        name=cfg.name or "",
        **cfg.template_vars(),
    )
    # Collapse consecutive separators left by an empty {name} placeholder.
    return re.sub(r"_+", "_", rendered).rstrip("_")


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
    for name in _PLANES:
        if (plane := getattr(f, name)) is not None:
            rec[name] = plane
    if f.rgb_intrinsics is not None:
        rec["rgb_intrinsics"] = f.rgb_intrinsics
    return rec


def _build_metadata(cfg: Any) -> dict[str, Any]:
    """Dump every config section, so a new section is included without editing this."""
    meta: dict[str, Any] = {"name": cfg.name}
    for field in type(cfg).model_fields:
        if field == "name":
            continue
        value = getattr(cfg, field)
        meta[field] = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    meta["created_at"] = datetime.now().isoformat()
    meta["version"] = _spad_capture_version()
    # ``sensor_layout`` is stamped later by the backend through ``update_layout``.
    return meta


# ---------------------------------------------------------------------------
# Base interface
# ---------------------------------------------------------------------------


class Writer(ABC):
    """Append-style frame writer. Creates a per-run subfolder under storage.root."""

    def __init__(self, cfg: Any):
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

    def __init__(self, cfg: Any):
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

    def __init__(self, cfg: Any):
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
    """Streamed HDF5 with extendable /frames, /timestamps, /metadata, optional camera planes."""

    def __init__(self, cfg: Any):
        super().__init__(cfg)
        import h5py
        self._h5py = h5py
        self._path = self.run_dir / f"{cfg.storage.data_filename}.h5"
        self._f = h5py.File(self._path, "w")
        self._f.create_group("metadata").attrs["json"] = json.dumps(self.metadata, default=str)
        self._frames = None
        self._planes: dict[str, Any] = {}

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
        for name, (dtype, _) in _PLANES.items():
            plane = getattr(frame, name)
            if plane is None or name in self._planes:
                continue
            self._planes[name] = self._f.create_dataset(
                name, shape=(0, *plane.shape), maxshape=(None, *plane.shape),
                dtype=dtype, chunks=(1, *plane.shape), compression="gzip",
            )
        if frame.rgb_intrinsics and "rgb_intrinsics" not in self._f:
            self._f.create_group("rgb_intrinsics").attrs["json"] = json.dumps(frame.rgb_intrinsics)

    def write(self, frame: Frame) -> None:
        self._ensure_datasets(frame)
        n = self._frames.shape[0]
        self._frames.resize(n + 1, axis=0); self._frames[n] = frame.histogram
        self._ts.resize(n + 1, axis=0);     self._ts[n] = frame.timestamp
        self._idx.resize(n + 1, axis=0);    self._idx[n] = frame.index
        for name, ds in self._planes.items():
            if (plane := getattr(frame, name)) is not None:
                ds.resize(n + 1, axis=0); ds[n] = plane

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


# Optional camera planes a Frame may carry: name -> (hdf dtype, npz index key).
_PLANES = {
    "rgb_bgr":  ("u1", "rgb_at"),
    "depth_mm": ("u2", "depth_at"),
    "ir_left":  ("u1", "ir_left_at"),
    "ir_right": ("u1", "ir_right_at"),
}


class NpzWriter(Writer):
    """In-memory buffer flushed to a single data.npz at close."""

    def __init__(self, cfg: Any):
        super().__init__(cfg)
        self._path = self.run_dir / f"{cfg.storage.data_filename}.npz"
        self._buf: list[Frame] = []

    @property
    def path(self) -> Path:
        return self._path

    def write(self, frame: Frame) -> None:
        self._buf.append(frame)

    def close(self) -> None:
        if not self._buf:
            return
        arrays: dict[str, np.ndarray] = {
            "histograms": np.stack([f.histogram for f in self._buf]),
            "index": np.array([f.index for f in self._buf], dtype=np.int64),
            "device_index": np.array([f.device_index for f in self._buf], dtype=np.int64),
            "timestamp": np.array([f.timestamp for f in self._buf], dtype=np.float64),
            "device_ts_ms": np.array([f.device_ts_ms for f in self._buf], dtype=np.int64),
        }
        if all(f.ambient is not None for f in self._buf):
            arrays["ambient"] = np.stack([f.ambient for f in self._buf]).astype(np.float32)
        # Index-aligned camera planes: only frames carrying a modality contribute a
        # plane plus its position, so a 20-frame burst with one attached image
        # stores a single plane, not 20 copies.
        for key, (_, at_key) in _PLANES.items():
            planes = [getattr(f, key, None) for f in self._buf]
            at = [i for i, pl in enumerate(planes) if pl is not None]
            if at:
                arrays[key] = np.stack([planes[i] for i in at])
                arrays[at_key] = np.array(at, dtype=np.int64)
        # Camera intrinsics are a constant; keep them in metadata, not the npz.
        intrinsics = next(
            (i for f in self._buf if (i := f.rgb_intrinsics) is not None), None)
        if intrinsics is not None and self.metadata.get("rgb_intrinsics") != intrinsics:
            self.metadata["rgb_intrinsics"] = intrinsics
            self._rewrite_metadata()
        np.savez(self._path, **arrays)
        self._buf.clear()


_WRITERS: dict[str, type[Writer]] = {}


def register_writer(fmt: str, cls: type[Writer]) -> None:
    """Register a writer for a ``storage.format`` value."""
    _WRITERS[fmt] = cls


def make_writer(cfg: Any) -> Writer:
    """Build the writer instance from a config."""
    fmt = getattr(cfg.storage.format, "value", cfg.storage.format)
    try:
        cls = _WRITERS[fmt]
    except KeyError:
        raise ValueError(
            f"No writer registered for storage format {fmt!r}. "
            f"Available: {', '.join(sorted(_WRITERS))}"
        ) from None
    return cls(cfg)


for _fmt, _cls in (("pkl", PklWriter), ("npy", NpyWriter), ("h5", HdfWriter),
                   ("npz", NpzWriter), ("none", NullWriter)):
    register_writer(_fmt, _cls)


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
            planes = {k: f[k][:] for k in _PLANES if k in f}
        frames = []
        for i in range(hists.shape[0]):
            r = {"index": int(idx[i]), "timestamp": float(ts[i]), "histogram": hists[i]}
            for k, arr in planes.items():
                r[k] = arr[i]
            frames.append(r)
        return _prefer_sidecar(metadata), frames
    if ext == "npz":
        with np.load(p) as z:
            data = {k: z[k] for k in z.files}
        metadata = json.loads(sidecar.read_text()) if sidecar.exists() else {}
        at = {name: dict(zip(data[key].tolist(), data[name]))
              for name, (_, key) in _PLANES.items() if name in data and key in data}
        frames = []
        for i in range(data["histograms"].shape[0]):
            r = {"index": int(data["index"][i]),
                 "timestamp": float(data["timestamp"][i]),
                 "histogram": data["histograms"][i]}
            if "ambient" in data:
                r["ambient"] = data["ambient"][i]
            for name, by_frame in at.items():
                if i in by_frame:
                    r[name] = by_frame[i]
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
