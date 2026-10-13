import csv
import hashlib
import hmac
import io
import itertools
import json
import logging
import os
import re
import secrets
import sqlite3
import statistics
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import blob_source
from . import config
from . import db
from .dicom_import import Importer, new_code
from .idrf import GROUPS, ITEM_IDS, SHORT, VALID

log = logging.getLogger("idrf")
DATA_DIR = config.data_dir()
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
DEFAULT_PW = "CHANGE-THIS-PASSWORD"
SESSION_HOURS = float(os.environ.get("SESSION_HOURS", "12"))
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "0") == "1"
STATIC = Path(__file__).parent / "static"
COOKIE = "idrf_session"
PBKDF2_ITER = 200_000

try:
    for sub in ("", "dicom", "cache"):
        (DATA_DIR / sub).mkdir(parents=True, exist_ok=True)
    db.init(DATA_DIR / "idrf.db")
except PermissionError:
    sys.exit(f"IDRF Reader cannot write to {DATA_DIR}. Check that this user can write to that folder, or set DATA_DIR in .env.")
importer = Importer(DATA_DIR)


@asynccontextmanager
async def lifespan(_app):
    if not ADMIN_PASSWORD or ADMIN_PASSWORD == DEFAULT_PW:
        log.warning("ADMIN_PASSWORD is not set: the coordinator account cannot sign in until it is set in .env.")
    importer.start()  # pick up any folders already in data/dicom
    yield


app = FastAPI(title="IDRF Reader", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def audit(who, action, detail=""):
    with db.connect() as c:
        c.execute("INSERT INTO audit(at, who, action, detail) VALUES(?, ?, ?, ?)", (now_iso(), who, action, detail))


# ------------------------------------------------------------------ passwords and sessions
def hash_pw(pw):
    salt = secrets.token_bytes(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, PBKDF2_ITER)
    return f"pbkdf2${PBKDF2_ITER}${salt.hex()}${h.hex()}"


def check_pw(pw, stored):
    try:
        _, it, salt, h = stored.split("$")
        calc = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), int(it))
        return hmac.compare_digest(calc.hex(), h)
    except Exception:
        return False


def get_user(request: Request):
    tok = request.cookies.get(COOKIE)
    if not tok:
        return None
    with db.connect() as c:
        s = c.execute("SELECT * FROM sessions WHERE token=?", (tok,)).fetchone()
        if not s or s["expires"] < time.time():
            return None
        if s["is_admin"]:
            return {"is_admin": True, "reader_id": None, "name": "Study coordinator", "role": "admin", "username": "admin"}
        r = c.execute("SELECT * FROM readers WHERE id=? AND active=1", (s["reader_id"],)).fetchone()
    if not r:
        return None
    return {"is_admin": False, "reader_id": r["id"], "name": r["name"], "role": r["role"], "username": r["username"]}


def need_user(request: Request):
    u = get_user(request)
    if not u:
        raise HTTPException(401, "Please sign in again.")
    return u


def need_reader(u=Depends(need_user)):
    if u["is_admin"]:
        raise HTTPException(403, "The coordinator account cannot score cases. Sign in with a reader account.")
    return u


def need_admin(u=Depends(need_user)):
    if not u["is_admin"]:
        raise HTTPException(403, "This page is for the study coordinator.")
    return u


# ------------------------------------------------------------------ pages
@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"ok": True}


@app.get("/", include_in_schema=False)
def root(request: Request):
    u = get_user(request)
    if not u:
        return RedirectResponse("/login")
    return RedirectResponse("/admin" if u["is_admin"] else "/read")


@app.get("/login", include_in_schema=False)
def login_page():
    return FileResponse(STATIC / "login.html")


@app.get("/read", include_in_schema=False)
def read_page(request: Request):
    u = get_user(request)
    if not u or u["is_admin"]:
        return RedirectResponse("/login")
    return FileResponse(STATIC / "reader.html")


@app.get("/admin", include_in_schema=False)
def admin_page(request: Request):
    u = get_user(request)
    if not u or not u["is_admin"]:
        return RedirectResponse("/login")
    return FileResponse(STATIC / "admin.html")


# ------------------------------------------------------------------ auth API
class LoginIn(BaseModel):
    username: str
    password: str


@app.post("/api/login")
def login(body: LoginIn, response: Response):
    uname = body.username.strip().lower()
    ok, reader_id, is_admin = False, None, False
    if uname == "admin":
        if not ADMIN_PASSWORD or ADMIN_PASSWORD == DEFAULT_PW:
            raise HTTPException(400, "The coordinator password has not been set. Set ADMIN_PASSWORD in the .env file and restart.")
        ok, is_admin = hmac.compare_digest(body.password.encode(), ADMIN_PASSWORD.encode()), True
    else:
        with db.connect() as c:
            r = c.execute("SELECT * FROM readers WHERE lower(username)=? AND active=1", (uname,)).fetchone()
        if r and check_pw(body.password, r["pw_hash"]):
            ok, reader_id = True, r["id"]
    if not ok:
        time.sleep(0.6)
        audit(uname or "?", "login_failed")
        raise HTTPException(401, "Wrong username or password.")
    tok = secrets.token_urlsafe(32)
    with db.connect() as c:
        c.execute("DELETE FROM sessions WHERE expires < ?", (time.time(),))
        c.execute("INSERT INTO sessions(token, reader_id, is_admin, expires) VALUES(?, ?, ?, ?)",
                  (tok, reader_id, int(is_admin), time.time() + SESSION_HOURS * 3600))
    response.set_cookie(COOKIE, tok, httponly=True, samesite="lax", secure=COOKIE_SECURE,
                        max_age=int(SESSION_HOURS * 3600), path="/")
    audit(uname, "login")
    return {"redirect": "/admin" if is_admin else "/read"}


@app.post("/api/logout")
def logout(request: Request, response: Response):
    tok = request.cookies.get(COOKIE)
    if tok:
        with db.connect() as c:
            c.execute("DELETE FROM sessions WHERE token=?", (tok,))
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@app.get("/api/me")
def me(u=Depends(need_user)):
    return u


@app.get("/api/idrf")
def idrf_items(u=Depends(need_user)):
    return {"groups": GROUPS}


# ------------------------------------------------------------------ reader API
def assigned(c, u):
    q = "SELECT id, code FROM cases WHERE status='ready'" + ("" if u["role"] == "primary" else " AND double_read=1")
    salt = db.meta_get(c, "order_salt", "")
    rows = c.execute(q).fetchall()
    return sorted(rows, key=lambda r: hashlib.sha256(f'{salt}:{u["reader_id"]}:{r["code"]}'.encode()).hexdigest())


def case_for(c, u, code):
    r = c.execute("SELECT * FROM cases WHERE code=? AND status='ready'", (code,)).fetchone()
    if not r or (u["role"] != "primary" and not r["double_read"]):
        raise HTTPException(404, "This case is not on your list.")
    return r


@app.get("/api/worklist")
def worklist(u=Depends(need_reader)):
    with db.connect() as c:
        rows = assigned(c, u)
        saved = {r["case_id"] for r in c.execute("SELECT case_id FROM reads WHERE reader_id=? AND saved=1", (u["reader_id"],))}
    cases = [{"code": r["code"], "saved": r["id"] in saved} for r in rows]
    return {"name": u["name"], "cases": cases, "next": next((i for i, x in enumerate(cases) if not x["saved"]), None)}


@app.get("/api/case/{code}/meta")
def case_meta(code: str, u=Depends(need_reader)):
    with db.connect() as c:
        r = case_for(c, u, code)
    m = json.loads(r["meta"] or "{}")
    return {"code": code, **{k: m[k] for k in ("nx", "ny", "nz", "sx", "sy", "sz")}}


@app.get("/api/case/{code}/volume")
def case_volume(code: str, u=Depends(need_reader)):
    with db.connect() as c:
        r = case_for(c, u, code)
    p = DATA_DIR / "cache" / f'{r["code"]}.bin.gz'
    if not p.exists():
        raise HTTPException(404, "Images for this case are not prepared. Ask the coordinator to scan for images again.")
    return FileResponse(p, media_type="application/octet-stream", headers={"Cache-Control": "private, no-store"})


class DraftIn(BaseModel):
    answers: Dict[str, str] = {}
    confidence: Optional[int] = None
    notes: str = ""
    seconds: float = 0


def _upsert(c, reader_id, case_id, body):
    a = {k: v for k, v in body.answers.items() if k in ITEM_IDS and v in VALID}
    conf = body.confidence if body.confidence in (1, 2, 3, 4, 5) else None
    c.execute(
        """INSERT INTO reads(reader_id, case_id, answers, confidence, notes, seconds, updated_at)
           VALUES(?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(reader_id, case_id) DO UPDATE SET answers=excluded.answers, confidence=excluded.confidence,
             notes=excluded.notes, seconds=MAX(reads.seconds, excluded.seconds), updated_at=excluded.updated_at""",
        (reader_id, case_id, json.dumps(a), conf, body.notes[:4000], max(0.0, float(body.seconds)), now_iso()))
    return a


@app.get("/api/case/{code}/read")
def get_read(code: str, u=Depends(need_reader)):
    with db.connect() as c:
        r = case_for(c, u, code)
        x = c.execute("SELECT * FROM reads WHERE reader_id=? AND case_id=?", (u["reader_id"], r["id"])).fetchone()
    if not x:
        return {"answers": {}, "confidence": None, "notes": "", "seconds": 0, "saved": False}
    return {"answers": json.loads(x["answers"]), "confidence": x["confidence"], "notes": x["notes"],
            "seconds": x["seconds"], "saved": bool(x["saved"])}


@app.post("/api/case/{code}/draft")
def save_draft(code: str, body: DraftIn, u=Depends(need_reader)):
    with db.connect() as c:
        r = case_for(c, u, code)
        _upsert(c, u["reader_id"], r["id"], body)
    return {"ok": True}


@app.post("/api/case/{code}/save")
def save_read(code: str, body: DraftIn, u=Depends(need_reader)):
    missing = [i for i in ITEM_IDS if body.answers.get(i) not in VALID]
    with db.connect() as c:
        r = case_for(c, u, code)
        _upsert(c, u["reader_id"], r["id"], body)
        if not missing:
            c.execute("UPDATE reads SET saved=1, saved_at=? WHERE reader_id=? AND case_id=?", (now_iso(), u["reader_id"], r["id"]))
    if missing:
        raise HTTPException(400, f"{len(missing)} item(s) still need an answer.")
    audit(u["username"], "save", code)
    return {"ok": True}


# ------------------------------------------------------------------ coordinator API
def _kappa(pairs):
    n = agree = p1 = p2 = 0
    per = {i: [0, 0] for i in ITEM_IDS}
    for A, B in pairs:
        for i in ITEM_IDS:
            x, y = A.get(i), B.get(i)
            if x not in ("absent", "present") or y not in ("absent", "present"):
                continue
            X, Y = x == "present", y == "present"
            n += 1
            agree += X == Y
            p1 += X
            p2 += Y
            per[i][0] += X == Y
            per[i][1] += 1
    if not n:
        return {"items": 0, "agreement": None, "kappa": None, "per_item": []}
    po, q1, q2 = agree / n, p1 / n, p2 / n
    pe = q1 * q2 + (1 - q1) * (1 - q2)
    return {"items": n, "agreement": po, "kappa": None if pe >= 1 else (po - pe) / (1 - pe),
            "per_item": [{"item": SHORT[i], "agree": a, "n": m} for i, (a, m) in per.items() if m]}


@app.get("/api/admin/overview")
def overview(u=Depends(need_admin)):
    with db.connect() as c:
        cases = [dict(r) for r in c.execute("SELECT id, code, folder, patient, timepoint, double_read, status, message, meta, imported_at, source FROM cases ORDER BY folder")]
        readers = [dict(r) for r in c.execute("SELECT id, username, name, role, active, created_at FROM readers ORDER BY id")]
        reads = [dict(r) for r in c.execute("SELECT reader_id, case_id, answers, seconds FROM reads WHERE saved=1")]
    for cs in cases:
        m = json.loads(cs.pop("meta") or "{}")
        cs["series"] = m.get("series_desc", "")
        cs["size"] = f'{m["nx"]} × {m["ny"]} × {m["nz"]}' if m.get("nx") else ""
    ready = [x for x in cases if x["status"] == "ready"]
    byr = {}
    for r in reads:
        byr.setdefault(r["reader_id"], {})[r["case_id"]] = (json.loads(r["answers"]), r["seconds"])

    progress = []
    for rd in readers:
        n_assigned = sum(1 for x in ready if rd["role"] == "primary" or x["double_read"])
        mine = byr.get(rd["id"], {})
        times = [s for _, s in mine.values()]
        med = statistics.median(times) if times else None
        progress.append({"id": rd["id"], "name": rd["name"], "role": rd["role"], "active": rd["active"],
                         "assigned": n_assigned, "saved": len(mine), "median_seconds": med,
                         "remaining_seconds": (med * max(0, n_assigned - len(mine))) if med else None})

    names = {rd["id"]: rd["name"] for rd in readers}
    agreement = []
    for a, b in itertools.combinations([rd["id"] for rd in readers], 2):
        common = set(byr.get(a, {})) & set(byr.get(b, {}))
        if common:
            k = _kappa([(byr[a][cid][0], byr[b][cid][0]) for cid in common])
            agreement.append({"a": names[a], "b": names[b], "cases": len(common), **k})

    prim = [rd for rd in readers if rd["role"] == "primary"]
    ref = max(prim, key=lambda rd: len(byr.get(rd["id"], {})), default=None)
    ref_reads = byr.get(ref["id"], {}) if ref else {}
    by_patient = {}
    for cs in ready:
        if cs["patient"] and cs["timepoint"] in ("diagnosis", "post_induction"):
            by_patient.setdefault(cs["patient"], {})[cs["timepoint"]] = cs
    pairs = []
    for pid in sorted(by_patient):
        tp = by_patient[pid]
        d, p = tp.get("diagnosis"), tp.get("post_induction")
        row = {"patient": pid, "diagnosis": d["code"] if d else None, "post": p["code"] if p else None}
        ra = ref_reads.get(d["id"]) if d else None
        rb = ref_reads.get(p["id"]) if p else None
        if ra and rb:
            na = [i for i in ITEM_IDS if ra[0].get(i) == "present"]
            nb = [i for i in ITEM_IDS if rb[0].get(i) == "present"]
            row.update(before=len(na), after=len(nb), cleared=bool(na) and not nb,
                       persisting=[SHORT[i] for i in na if i in nb], new=[SHORT[i] for i in nb if i not in na])
        pairs.append(row)

    return {"import": importer.status(), "cases": cases, "readers": readers, "progress": progress,
            "agreement": agreement, "pairs": pairs, "pairs_reader": ref["name"] if ref else None}


@app.post("/api/admin/rescan")
def rescan(u=Depends(need_admin)):
    started = importer.start()
    audit("admin", "rescan")
    return {"started": started, "import": importer.status()}


@app.get("/api/admin/import-status")
def import_status(u=Depends(need_admin)):
    return importer.status()


class CasePatch(BaseModel):
    patient: Optional[str] = None
    timepoint: Optional[str] = None
    double_read: Optional[bool] = None


@app.patch("/api/admin/case/{cid}")
def patch_case(cid: int, body: CasePatch, u=Depends(need_admin)):
    sets, vals = [], []
    if body.patient is not None:
        sets.append("patient=?")
        vals.append(body.patient.strip()[:64] or None)
    if body.timepoint is not None:
        if body.timepoint not in ("", "diagnosis", "post_induction"):
            raise HTTPException(400, "Timepoint must be diagnosis or post_induction.")
        sets.append("timepoint=?")
        vals.append(body.timepoint or None)
    if body.double_read is not None:
        sets.append("double_read=?")
        vals.append(int(body.double_read))
    if sets:
        with db.connect() as c:
            c.execute(f"UPDATE cases SET {', '.join(sets)} WHERE id=?", (*vals, cid))
    return {"ok": True}


@app.delete("/api/admin/case/{cid}")
def delete_case(cid: int, u=Depends(need_admin)):
    with db.connect() as c:
        r = c.execute("SELECT * FROM cases WHERE id=?", (cid,)).fetchone()
        if not r:
            raise HTTPException(404, "This case no longer exists.")
        if r["source"] != "blob":
            raise HTTPException(400, "To remove a scan from data/dicom, delete its folder and scan again.")
        if c.execute("SELECT 1 FROM reads WHERE case_id=?", (cid,)).fetchone():
            raise HTTPException(400, "A reader has already started this case, so it cannot be removed.")
        c.execute("DELETE FROM cases WHERE id=?", (cid,))
    (DATA_DIR / "cache" / f'{r["code"]}.bin.gz').unlink(missing_ok=True)
    audit("admin", "remove_case", f'{r["code"]} {r["folder"]}')
    return {"ok": True}


# ------------------------------------------------------------------ cloud storage
def _cloud_studies():
    if not blob_source.enabled():
        raise HTTPException(400, "Cloud storage is not set up. Set AZURE_STORAGE_CONNECTION_STRING and restart.")
    try:
        return blob_source.list_studies()
    except Exception as e:
        log.exception("cloud storage listing failed")
        raise HTTPException(502, f"Could not read cloud storage: {str(e).splitlines()[0][:300]}")


@app.get("/api/admin/cloud")
def cloud_list(u=Depends(need_admin)):
    if not blob_source.enabled():
        return {"enabled": False, "studies": []}
    return {"enabled": True, "where": blob_source.where(), "studies": _cloud_studies()}


class CloudAdd(BaseModel):
    studies: List[str]


@app.post("/api/admin/cloud/add")
def cloud_add(body: CloudAdd, u=Depends(need_admin)):
    found = {s["path"]: s for s in _cloud_studies()}
    added = 0
    with db.connect() as c:
        codes = {r["code"] for r in c.execute("SELECT code FROM cases")}
        for path in dict.fromkeys(body.studies):
            s = found.get(path)
            if not s or c.execute("SELECT 1 FROM cases WHERE folder=?", (path,)).fetchone():
                continue
            code = new_code(codes)
            codes.add(code)
            c.execute("INSERT INTO cases(code, folder, patient, status, source) VALUES(?, ?, ?, 'pending', 'blob')",
                      (code, path, s["patient"][:64]))
            added += 1
    audit("admin", "cloud_add", f"{added} studies")
    if added:
        importer.start()
    return {"added": added, "import": importer.status()}


class ReaderIn(BaseModel):
    username: str
    name: str
    password: str
    role: str = "primary"


class ReaderPatch(BaseModel):
    name: Optional[str] = None
    role: Optional[str] = None
    active: Optional[bool] = None
    password: Optional[str] = None


def _check_pw_rules(pw):
    if len(pw) < 8:
        raise HTTPException(400, "Passwords need at least 8 characters.")


@app.post("/api/admin/readers")
def add_reader(body: ReaderIn, u=Depends(need_admin)):
    uname = body.username.strip().lower()
    if not re.fullmatch(r"[a-z0-9._-]{2,32}", uname) or uname == "admin":
        raise HTTPException(400, "Usernames use 2 to 32 lowercase letters, numbers, dots, dashes or underscores, and cannot be admin.")
    if body.role not in ("primary", "second"):
        raise HTTPException(400, "Role must be primary or second.")
    if not body.name.strip():
        raise HTTPException(400, "Add the reader's name.")
    _check_pw_rules(body.password)
    try:
        with db.connect() as c:
            c.execute("INSERT INTO readers(username, name, pw_hash, role, created_at) VALUES(?, ?, ?, ?, ?)",
                      (uname, body.name.strip()[:80], hash_pw(body.password), body.role, now_iso()))
    except sqlite3.IntegrityError:
        raise HTTPException(400, "That username is already taken.")
    audit("admin", "add_reader", uname)
    return {"ok": True}


@app.patch("/api/admin/readers/{rid}")
def patch_reader(rid: int, body: ReaderPatch, u=Depends(need_admin)):
    sets, vals = [], []
    if body.name is not None and body.name.strip():
        sets.append("name=?")
        vals.append(body.name.strip()[:80])
    if body.role is not None:
        if body.role not in ("primary", "second"):
            raise HTTPException(400, "Role must be primary or second.")
        sets.append("role=?")
        vals.append(body.role)
    if body.active is not None:
        sets.append("active=?")
        vals.append(int(body.active))
    if body.password:
        _check_pw_rules(body.password)
        sets.append("pw_hash=?")
        vals.append(hash_pw(body.password))
    if sets:
        with db.connect() as c:
            c.execute(f"UPDATE readers SET {', '.join(sets)} WHERE id=?", (*vals, rid))
            if body.active is False or body.password:
                c.execute("DELETE FROM sessions WHERE reader_id=?", (rid,))
    audit("admin", "edit_reader", str(rid))
    return {"ok": True}


@app.get("/api/admin/export.csv")
def export_csv(u=Depends(need_admin)):
    with db.connect() as c:
        rows = c.execute(
            """SELECT r.*, rd.username, rd.role, cs.code, cs.folder, cs.patient, cs.timepoint
               FROM reads r JOIN readers rd ON rd.id = r.reader_id JOIN cases cs ON cs.id = r.case_id
               WHERE r.saved = 1 ORDER BY cs.patient, cs.timepoint, rd.id""").fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["reader", "reader_role", "case_code", "folder", "patient", "timepoint", "seconds", "confidence",
                "present_count", *ITEM_IDS, "notes", "saved_at"])
    for r in rows:
        a = json.loads(r["answers"])
        w.writerow([r["username"], r["role"], r["code"], r["folder"], r["patient"] or "", r["timepoint"] or "",
                    round(r["seconds"]), r["confidence"] or "", sum(a.get(i) == "present" for i in ITEM_IDS),
                    *[a.get(i, "") for i in ITEM_IDS], r["notes"], r["saved_at"]])
    audit("admin", "export")
    name = f'idrf_reads_{datetime.now().strftime("%Y-%m-%d")}.csv'
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})
