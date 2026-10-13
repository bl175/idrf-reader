"""Scans data/dicom/<case folder>/ for CT series, picks the contrast-enhanced axial series,
converts it to a Hounsfield-unit volume and stores it as gzip-compressed int16 for the viewer.

Only pixel data and voxel spacing ever reach the browser. DICOM header fields (names, IDs,
dates, institution) are read on the server and never sent to readers.
"""
import csv
import gzip
import json
import os
import secrets
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pydicom

from . import blob_source
from . import config  # noqa: F401  (loads .env before settings are read)
from . import db

ALPH = "23456789ABCDEFGHJKMNPQRSTVWXYZ"  # no 0/O, 1/I/L, U
CONTRAST_WORDS = ("portal", "venous", " pv", "pv ", "c+", "+c", "contrast", "enhanc", "cect", "post con", "postcon")
NONCON_WORDS = ("non con", "noncon", "non-con", "plain", "w/o", "without", "unenhanced", "pre con", "precon", "nc ")
TIMEPOINTS = {
    "diagnosis": "diagnosis", "baseline": "diagnosis", "dx": "diagnosis", "pre": "diagnosis",
    "post_induction": "post_induction", "post": "post_induction", "post_chemo": "post_induction",
    "after": "post_induction", "preop": "post_induction", "pre_op": "post_induction",
}
PREFERRED_THICKNESS = float(os.environ.get("PREFERRED_THICKNESS", "2.5"))


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_code(existing):
    while True:
        c = "NB-" + "".join(secrets.choice(ALPH) for _ in range(4))
        if c not in existing:
            return c


def folder_signature(folder):
    n = size = 0
    mt = 0.0
    for root, _, files in os.walk(folder):
        for f in files:
            st = os.stat(os.path.join(root, f))
            n += 1
            size += st.st_size
            mt = max(mt, st.st_mtime)
    return f"{n}:{size}:{int(mt)}"


def read_manifest(path):
    """Optional data/cases.csv with columns: folder, patient, timepoint, double_read, series.
    Empty cells are ignored, so values set in the coordinator page are kept."""
    out = {}
    if not path.exists():
        return out
    with open(path, newline="", encoding="utf-8-sig") as f:
        for raw in csv.DictReader(f):
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
            folder = row.get("folder")
            if not folder:
                continue
            tp = row.get("timepoint", "").lower().replace("-", "_").replace(" ", "_")
            dr = row.get("double_read", "").lower()
            out[folder] = {
                "patient": row.get("patient") or None,
                "timepoint": TIMEPOINTS.get(tp) if tp else None,
                "double_read": (dr in ("1", "yes", "y", "true")) if dr else None,
                "series": row.get("series") or None,
            }
    return out


def _describe(s):
    return f'{s["desc"] or "(no description)"} ({len(s["files"])} images)'


def _score(s):
    """Contrast-enhanced first; then slice thickness closest to the usual reading thickness
    (so a 0.6 mm thin reconstruction does not win just by having more images); then most images."""
    d = s["desc"].lower()
    sc = 0
    if any(w in d for w in NONCON_WORDS):
        sc -= 2
    if any(w in d for w in CONTRAST_WORDS):
        sc += 2
    if s["contrast"]:
        sc += 1
    thk = s.get("thk") or 0
    closeness = -round(abs(thk - PREFERRED_THICKNESS) * 2) / 2 if thk else -99
    return (sc, closeness, len(s["files"]))


def build_volume(folder, out_path, hint="", max_inplane=512):
    series, multiframe = {}, 0
    for p in sorted(Path(folder).rglob("*")):
        if not p.is_file() or p.name.upper() == "DICOMDIR" or p.name.startswith("."):
            continue
        try:
            ds = pydicom.dcmread(str(p), stop_before_pixels=True, force=True)
        except Exception:
            continue
        uid = getattr(ds, "SeriesInstanceUID", None)
        if not uid:
            continue
        try:
            nf = int(getattr(ds, "NumberOfFrames", 1) or 1)
        except (TypeError, ValueError):
            nf = 1
        if nf > 1:
            multiframe += 1
            continue
        ipp = getattr(ds, "ImagePositionPatient", None)
        iop = getattr(ds, "ImageOrientationPatient", None)
        if ipp is None or iop is None or len(ipp) != 3 or len(iop) != 6:
            continue
        s = series.setdefault(str(uid), {
            "uid": str(uid),
            "desc": str(getattr(ds, "SeriesDescription", "") or ""),
            "modality": str(getattr(ds, "Modality", "") or ""),
            "iop": [float(x) for x in iop],
            "contrast": bool(str(getattr(ds, "ContrastBolusAgent", "") or "").strip()),
            "thk": float(getattr(ds, "SliceThickness", 0) or 0),
            "files": [],
        })
        s["files"].append((p, [float(x) for x in ipp]))

    def axial(s):
        n = np.cross(np.array(s["iop"][:3]), np.array(s["iop"][3:]))
        return abs(n[2]) > 0.9

    warnings = []
    if multiframe:
        warnings.append(f"{multiframe} multi-frame file(s) skipped. Export as classic single-frame CT if they are needed.")
    cands = [s for s in series.values() if s["modality"] in ("CT", "") and axial(s) and len(s["files"]) >= 10]
    if hint:
        h = hint.lower()
        cands = [s for s in cands if s["uid"] == hint or h in s["desc"].lower()]
        if not cands:
            raise ValueError(f'No axial CT series matches "{hint}". Series found: '
                             + ("; ".join(_describe(s) for s in series.values()) or "none"))
    if not cands:
        raise ValueError("No axial CT series with at least 10 images was found in this folder. Series found: "
                         + ("; ".join(_describe(s) for s in series.values()) or "none"))
    cands.sort(key=_score, reverse=True)
    s = cands[0]
    if len(cands) > 1:
        warnings.append(f'Several axial CT series found; using {_describe(s)}. Others: '
                        + "; ".join(_describe(x) for x in cands[1:4])
                        + ". To choose another, put part of its name in the series column of cases.csv.")
    if _score(s)[0] <= 0:
        warnings.append("The chosen series does not look contrast-enhanced from its description. Please check it.")

    # superior slice first (DICOM +z is superior)
    items = sorted(((ipp[2], p) for p, ipp in s["files"]), key=lambda t: -t[0])
    uniq, seen = [], set()
    for z, p in items:
        k = round(z, 2)
        if k not in seen:
            seen.add(k)
            uniq.append((z, p))
    zs = np.array([z for z, _ in uniq])
    d = np.abs(np.diff(zs))
    sz = float(np.median(d)) if len(d) else 1.0
    if len(d) and (d.max() - d.min()) > 0.1 * sz + 0.01:
        warnings.append("Slice spacing is irregular, so measurements along the body axis may be slightly off.")

    first = pydicom.dcmread(str(uniq[0][1]), force=True)
    rows, cols = int(first.Rows), int(first.Columns)
    ps = [float(x) for x in getattr(first, "PixelSpacing", [1.0, 1.0])]
    sy, sx = ps[0], ps[1]
    vol = np.empty((len(uniq), rows, cols), dtype=np.int16)
    kept = skipped = 0
    for i, (_, p) in enumerate(uniq):
        ds = first if i == 0 else pydicom.dcmread(str(p), force=True)
        if int(ds.Rows) != rows or int(ds.Columns) != cols:
            skipped += 1
            continue
        arr = ds.pixel_array.astype(np.float32)
        slope = float(getattr(ds, "RescaleSlope", 1) or 1)
        inter = float(getattr(ds, "RescaleIntercept", 0) or 0)
        arr = arr * slope + inter
        np.clip(arr, -1024, 3071, out=arr)
        vol[kept] = arr.astype(np.int16)
        kept += 1
    if skipped:
        warnings.append(f"{skipped} image(s) with a different matrix size were skipped.")
    if kept < 10:
        raise ValueError("Fewer than 10 usable images after reading pixel data.")
    vol = vol[:kept]

    # radiological display: patient left on screen right, anterior at top
    if s["iop"][0] < 0:
        vol = vol[:, :, ::-1]
    if s["iop"][4] < 0:
        vol = vol[:, ::-1, :]

    f = int(np.ceil(max(rows, cols) / max_inplane)) if max_inplane > 0 else 1
    if f > 1:
        ny2, nx2 = rows // f, cols // f
        vol = vol[:, :ny2 * f, :nx2 * f].reshape(kept, ny2, f, nx2, f).mean(axis=(2, 4)).astype(np.int16)
        sy, sx = sy * f, sx * f
        warnings.append(f"Images reduced {f}x in-plane to {nx2}x{ny2} (MAX_INPLANE={max_inplane}).")

    vol = np.ascontiguousarray(vol, dtype="<i2")
    nz, ny, nx = vol.shape
    tmp = Path(str(out_path) + ".tmp")
    with gzip.open(tmp, "wb", compresslevel=4) as g:
        g.write(vol.tobytes())
    os.replace(tmp, out_path)
    return {"nx": nx, "ny": ny, "nz": nz, "sx": sx, "sy": sy, "sz": sz,
            "series_desc": s["desc"], "series_uid": s["uid"], "warnings": warnings}


class Importer:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.lock = threading.Lock()
        self.again = False
        self.state = {"running": False, "done": 0, "total": 0, "current": "", "started": None, "finished": None, "log": []}

    def status(self):
        with self.lock:
            return json.loads(json.dumps(self.state))

    def start(self):
        """Start a scan. If one is running, run another as soon as it ends, so newly added cases are picked up."""
        with self.lock:
            if self.state["running"]:
                self.again = True
                return False
            self.state.update(running=True, done=0, total=0, current="", started=time.time(), finished=None, log=[])
        threading.Thread(target=self._run, daemon=True).start()
        return True

    def _log(self, msg):
        with self.lock:
            self.state["log"] = (self.state["log"] + [msg])[-300:]

    def _run(self):
        try:
            while True:
                try:
                    self._scan()
                except Exception as e:
                    self._log(f"Scan stopped: {e}")
                with self.lock:
                    if not self.again:
                        break
                    self.again = False
                    self.state.update(done=0, total=0)
        finally:
            with self.lock:
                self.state.update(running=False, current="", finished=time.time())

    def _scan(self):
        root, cache = self.data_dir / "dicom", self.data_dir / "cache"
        root.mkdir(exist_ok=True)
        cache.mkdir(exist_ok=True)
        manifest = read_manifest(self.data_dir / "cases.csv")
        folders = sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith("."))
        max_inplane = int(os.environ.get("MAX_INPLANE", "512"))
        with db.connect() as c:
            codes = {r["code"] for r in c.execute("SELECT code FROM cases")}
            known = {r["folder"]: dict(r) for r in c.execute("SELECT * FROM cases")}
        cloud = [r for r in known.values() if r["source"] == "blob"]
        with self.lock:
            self.state["total"] = len(folders) + len(cloud)

        seen = set()
        for f in folders:
            seen.add(f.name)
            row = known.get(f.name)
            if not row:
                with db.connect() as c:
                    code = new_code(codes)
                    codes.add(code)
                    c.execute("INSERT INTO cases(code, folder, status) VALUES(?, ?, 'pending')", (code, f.name))
                    row = dict(c.execute("SELECT * FROM cases WHERE folder=?", (f.name,)).fetchone())
            self._prepare(row, manifest, max_inplane, lambda: folder_signature(f),
                          lambda out, hint, f=f: build_volume(f, out, hint, max_inplane))

        for row in cloud:
            name = row["folder"]
            with db.connect() as c:
                if not c.execute("SELECT 1 FROM cases WHERE id=?", (row["id"],)).fetchone():
                    self._step()  # removed by the coordinator since the scan started
                    continue
            try:
                files = blob_source.study_files(name)
            except Exception as e:
                self._fail(row, f"Could not read cloud storage: {e}")
                continue
            if not files:
                if row["status"] != "missing":
                    with db.connect() as c:
                        c.execute("UPDATE cases SET status='missing', message='Study no longer found in cloud storage.' WHERE id=?", (row["id"],))
                self._log(f"{name}: not found in cloud storage")
                self._step()
                continue

            def build(out, hint, name=name, files=files):
                with tempfile.TemporaryDirectory(prefix="idrf-") as tmp:
                    size = blob_source.download(name, files, tmp)
                    self._log(f"{name}: downloaded {len(files)} files ({size / 1e6:.0f} MB)")
                    return build_volume(tmp, out, hint, max_inplane)

            self._prepare(row, manifest, max_inplane, lambda files=files: blob_source.signature(files), build)

        with db.connect() as c:
            for name, row in known.items():
                if row["source"] == "local" and name not in seen and row["status"] != "missing":
                    c.execute("UPDATE cases SET status='missing', message='Folder no longer found in data/dicom.' WHERE id=?", (row["id"],))

    def _step(self):
        with self.lock:
            self.state["done"] += 1

    def _fail(self, row, msg):
        with db.connect() as c:
            c.execute("UPDATE cases SET status='error', message=? WHERE id=?", (msg[:1500], row["id"]))
        self._log(f'{row["folder"]}: {msg}')
        self._step()

    def _prepare(self, row, manifest, max_inplane, signature, build):
        """Apply cases.csv values, then (re)build the volume unless its source is unchanged."""
        name = row["folder"]
        with self.lock:
            self.state["current"] = name
        m = manifest.get(name, {})
        upd = {k: v for k, v in (("patient", m.get("patient")), ("timepoint", m.get("timepoint")),
                                 ("double_read", m.get("double_read")), ("series_hint", m.get("series"))) if v is not None}
        if "double_read" in upd:
            upd["double_read"] = int(upd["double_read"])
        if upd:
            with db.connect() as c:
                c.execute(f"UPDATE cases SET {', '.join(k + '=?' for k in upd)} WHERE id=?", (*upd.values(), row["id"]))
            row.update(upd)
        hint = row.get("series_hint") or ""
        sig = signature() + "|" + hint + f"|{max_inplane}"
        out = self.data_dir / "cache" / f'{row["code"]}.bin.gz'
        if row.get("sig") == sig and row.get("status") == "ready" and out.exists():
            self._log(f"{name}: unchanged")
            self._step()
            return
        try:
            meta = build(out, hint)
        except Exception as e:  # keep going with the other cases
            self._fail(row, str(e))
            return
        with db.connect() as c:
            updated = c.execute("UPDATE cases SET status='ready', message=?, meta=?, sig=?, imported_at=? WHERE id=?",
                                (" ".join(meta["warnings"]), json.dumps(meta), sig, now_iso(), row["id"])).rowcount
        if not updated:  # removed while it was being prepared
            out.unlink(missing_ok=True)
            self._step()
            return
        self._log(f'{name}: ready as {row["code"]} ({meta["nx"]}x{meta["ny"]}x{meta["nz"]})')
        self._step()
