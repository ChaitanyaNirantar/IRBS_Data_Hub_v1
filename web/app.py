"""
IRBS Data Hub - one web app for the Harvest database
====================================================
Tabs
  Explore       analysis-ready datasets (irbs.vw_harvest_*), filter, Excel export
  Data quality  ETL rejects + quality issues (irbs.vw_etl_issue_queue), record decisions
  Data entry    enter a new sampling event; it is held for review (entry.submission)
  Review        reviewer approves / rejects pending entries (entry.usp_review_submission)

Sign-in uses SQL Server logins. The username and password are kept in this
process's memory for the browser session only (never on disk, in logs or in
cookies) and are dropped on sign-out, after IRBS_IDLE_MINUTES of inactivity,
or after IRBS_MAX_SESSION_HOURS. What each person can do is decided by SQL
Server permissions (see sql/49_roles_and_permissions.sql), not by the app.

IMPORTANT FOR HOSTING: sessions live in this process's memory, so run ONE
process (Posit Connect: Runtime > Max processes = 1). Threads are fine.

Run locally:   start_portal.bat   (or: python app.py)   -> http://localhost:8050
Posit Connect: see DEPLOY.md
Health check:  GET /healthz   (no sign-in, no database; for monitoring)

Environment variables (all optional) - see DEPLOY.md for the full list.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import re
import secrets
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd
from flask import Flask, Response, g, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

HERE = Path(__file__).resolve().parent
SHARED = HERE if (HERE / "portal_core.py").exists() else HERE.parent
sys.path.insert(0, str(SHARED))

from portal_core import (DATASETS, DEFAULT_DATABASE, DEFAULT_SERVER, REASONS,  # noqa: E402
                         STATUS_LABELS, _val, connect, pack_dataset, read_view)

APP_VERSION = "1.1.0"
STARTED = time.time()


# ---------------------------------------------------------------------
# Configuration (environment variables, validated once at start-up)
# ---------------------------------------------------------------------
def _int_env(name: str, default: int, lo: int, hi: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        v = int(raw)
    except ValueError:
        raise SystemExit(f"Environment variable {name} must be a whole number (got {raw!r}).")
    if not lo <= v <= hi:
        raise SystemExit(f"Environment variable {name} must be between {lo} and {hi} (got {v}).")
    return v


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


class Config:
    SERVER = os.environ.get("IRBS_SQL_SERVER", DEFAULT_SERVER)
    DATABASE = os.environ.get("IRBS_SQL_DATABASE", DEFAULT_DATABASE)
    IDLE_SECONDS = _int_env("IRBS_IDLE_MINUTES", 30, 5, 24 * 60) * 60
    MAX_SESSION_SECONDS = _int_env("IRBS_MAX_SESSION_HOURS", 12, 1, 72) * 3600
    MAX_SESSIONS = _int_env("IRBS_MAX_SESSIONS", 300, 10, 10000)
    QUERY_TIMEOUT = _int_env("IRBS_QUERY_TIMEOUT", 300, 10, 3600)
    CACHE_SECONDS = _int_env("IRBS_CACHE_MINUTES", 10, 0, 24 * 60) * 60
    LOGIN_MAX_FAILURES = _int_env("IRBS_LOGIN_MAX_FAILURES", 5, 1, 100)
    LOGIN_LOCK_SECONDS = _int_env("IRBS_LOGIN_LOCK_MINUTES", 15, 1, 24 * 60) * 60
    TRUST_PROXY = _bool_env("IRBS_TRUST_PROXY", True)        # Posit Connect / IIS / nginx in front
    FORCE_SECURE_COOKIE = _bool_env("IRBS_SECURE_COOKIES", False)
    LOG_LEVEL = os.environ.get("IRBS_LOG_LEVEL", "INFO").upper()
    MAX_REQUEST_BYTES = 2 * 1024 * 1024
    COOKIE = "irbs_hub_session"


# ---------------------------------------------------------------------
# Logging: one line per event on stdout (Posit Connect shows it under Logs).
# Passwords are never logged.
# ---------------------------------------------------------------------
logging.basicConfig(level=getattr(logging, Config.LOG_LEVEL, logging.INFO),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stdout)
log = logging.getLogger("irbs.hub")
audit = logging.getLogger("irbs.audit")


def client_ip() -> str:
    return request.remote_addr or "?"


# ---------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------
class AppError(Exception):
    def __init__(self, msg: str, code: int = 400):
        super().__init__(msg)
        self.code = code


def sql_message(exc: Exception) -> str:
    """Turn a pyodbc error into the sentence a person should read."""
    text = str(exc)
    if "HYT00" in text or "Query timeout expired" in text:
        return ("The database took too long to answer and the request was cancelled. "
                "Try narrower filters, or ask the administrator to check the server.")
    if "08001" in text or "08S01" in text or "Communication link failure" in text:
        return "Could not reach the database server. Check the network/VPN and try again."
    m = re.findall(r"\[SQL Server\]([^\[]+?)(?:\s*\(\d+\))?(?:\s*\(SQL\w+\))?[\"')]*$", text)
    if m:
        return m[-1].strip().strip('"').strip()
    m = re.findall(r"\[SQL Server\](.+?)\(\d+\)", text)
    if m:
        return m[-1].strip()
    return text.splitlines()[0][:400]


def is_db_error(exc: Exception) -> bool:
    try:
        import pyodbc
        return isinstance(exc, pyodbc.Error)
    except ImportError:
        return False


def rows(cur) -> list[dict]:
    while cur.description is None:
        if not cur.nextset():
            return []
    cols = [c[0] for c in cur.description]
    return [{c: _val(v) for c, v in zip(cols, r)} for r in cur.fetchall()]


# ---------------------------------------------------------------------
# Database access (one connection per request, with the session's login)
# ---------------------------------------------------------------------
class Repo:
    def __init__(self, user: str, password: str):
        self.user, self.password = user, password

    def _conn(self):
        conn = connect(Config.SERVER, Config.DATABASE, self.user or None, self.password,
                       quiet=True, query_timeout=Config.QUERY_TIMEOUT)
        conn.autocommit = True
        return conn

    def _q(self, sql: str, *params) -> list[dict]:
        t0 = time.time()
        conn = self._conn()
        try:
            cur = conn.cursor()
            cur.execute(sql, *params)
            out = rows(cur)
        finally:
            conn.close()
        took = time.time() - t0
        if took > 2:
            log.warning("slow query %.1fs rows=%d user=%s sql=%s", took, len(out), self.user,
                        " ".join(sql.split())[:120])
        return out

    # ---- session ----
    def test_login(self) -> None:
        self._conn().close()

    def me(self) -> dict:
        r = self._q("""
            SELECT SUSER_SNAME() AS login,
                   ISNULL(HAS_PERMS_BY_NAME('entry.usp_review_submission','OBJECT','EXECUTE'),0) AS can_review,
                   ISNULL(HAS_PERMS_BY_NAME('entry.usp_submit_harvest_event','OBJECT','EXECUTE'),0) AS can_submit,
                   ISNULL(HAS_PERMS_BY_NAME('irbs.usp_set_issue_review','OBJECT','EXECUTE'),0) AS can_qa_review,
                   ISNULL(HAS_PERMS_BY_NAME('irbs.vw_etl_issue_queue','OBJECT','SELECT'),0) AS can_qa_view,
                   ISNULL(HAS_PERMS_BY_NAME('entry.vw_submission','OBJECT','SELECT'),0) AS can_entry_view
        """)[0]
        r["pending"] = None
        if r["can_entry_view"]:
            r["pending"] = self._q("SELECT COUNT(*) AS n FROM entry.vw_submission WHERE status = N'PENDING'")[0]["n"]
        return r

    def view_permissions(self) -> dict:
        """Which dataset views this login may SELECT (used to guard the shared cache)."""
        parts = ", ".join(f"ISNULL(HAS_PERMS_BY_NAME('{d['view']}','OBJECT','SELECT'),0) AS [{d['key']}]"
                          for d in DATASETS)
        r = self._q(f"SELECT {parts}, ISNULL(HAS_PERMS_BY_NAME('irbs.vw_etl_issue_queue','OBJECT','SELECT'),0) AS [__qa]")[0]
        return {k: bool(v) for k, v in r.items()}

    # ---- explore ----
    def dataset_counts(self) -> list[dict]:
        out = []
        conn = self._conn()
        try:
            cur = conn.cursor()
            for ds in DATASETS:
                meta = {k: ds[k] for k in ("key", "view", "title", "replaces", "description", "notes")}
                try:
                    cur.execute(f"SELECT COUNT_BIG(*) FROM {ds['view']}")
                    meta["rows"] = int(cur.fetchone()[0])
                except Exception as exc:
                    meta["rows"], meta["unavailable"] = None, sql_message(exc)
                out.append(meta)
        finally:
            conn.close()
        return out

    def dataset(self, ds: dict) -> pd.DataFrame:
        conn = self._conn()
        try:
            return read_view(conn, ds)
        finally:
            conn.close()

    # ---- data quality ----
    QA_COLS = ["issue_key", "issue_kind", "issue_row_id", "etl_run_id", "phase", "source_table", "source_record_id",
               "issue_code", "issue_message", "business_key", "target_table", "target_record_id",
               "review_status", "is_closed", "priority", "assigned_to", "decision", "resolution_notes",
               "reviewed_by", "reviewed_at"]

    def qa_issues(self) -> dict:
        issues = self._q(f"SELECT {', '.join(self.QA_COLS)} FROM irbs.vw_etl_issue_queue")
        try:
            recon = self._q("SELECT phase, etl_run_id, status, runs_for_phase, source_row_count, loaded_row_count, "
                            "rejected_row_count, reject_rows_logged, quality_rows_logged, reconciliation_status "
                            "FROM irbs.vw_etl_run_reconciliation ORDER BY module, etl_run_id")
        except Exception:
            recon = []
        return {"issues": issues, "recon": recon}

    def qa_detail(self, key: str, kind: str | None = None, row_id=None,
                  target_table: str | None = None, target_id=None, source_id: str | None = None) -> dict:
        raw = None
        if kind == "REJECT" and row_id:
            r = self._q("SELECT raw_record FROM irbs.etl_reject WHERE etl_reject_id = ?", int(row_id))
            raw = r[0]["raw_record"] if r else None
        lineage = []
        if target_table and target_id:
            try:
                lineage = self._q("SELECT source_table, source_record_id, resolution_type, "
                                  "CASE WHEN source_record_id = ? THEN 1 ELSE 0 END AS is_issue_source_row "
                                  "FROM irbs.source_record_map WHERE target_table = ? AND target_record_id = ? "
                                  "ORDER BY is_issue_source_row DESC, source_record_id",
                                  source_id or "", target_table, int(target_id))
            except Exception:
                lineage = []
        try:
            history = self._q("SELECT changed_at, changed_by, review_status, priority, assigned_to, decision, "
                              "resolution_notes FROM irbs.vw_etl_issue_review_history WHERE issue_key = ? "
                              "ORDER BY changed_at DESC, etl_issue_review_log_id DESC", key)
        except Exception:
            history = []
        return {"raw_record": raw, "lineage": lineage, "history": history}

    def qa_set_review(self, keys, status, by, priority, assigned_to, decision, notes) -> int:
        r = self._q("EXEC irbs.usp_set_issue_review @issue_keys=?, @review_status=?, @changed_by=?, "
                    "@priority=?, @assigned_to=?, @decision=?, @resolution_notes=?",
                    json.dumps(list(keys)), status, by, priority, assigned_to, decision, notes)
        return int(r[0]["reviews_written"]) if r else 0

    # ---- data entry ----
    def lookups(self) -> dict:
        def safe(sql):
            try:
                return self._q(sql)
            except Exception:
                return []
        return {"species": safe("SELECT species_code, common_name FROM entry.vw_species ORDER BY species_code"),
                "gear": safe("SELECT gear_code, gear_name FROM entry.vw_gear ORDER BY gear_code"),
                "reach": [r["reach"] for r in safe("SELECT reach FROM entry.vw_reach ORDER BY reach")]}

    def check(self, payload: dict, submission_id=None) -> list[dict]:
        return self._q("EXEC entry.usp_check_harvest_event @payload=?, @submission_id=?",
                       json.dumps(payload), submission_id)

    def submit(self, payload: dict, by: str, note: str | None) -> int:
        r = self._q("EXEC entry.usp_submit_harvest_event @payload=?, @submitted_by=?, @note=?",
                    json.dumps(payload), by, note)
        return int(r[0]["submission_id"])

    def submissions(self) -> list[dict]:
        return self._q("SELECT TOP (2000) submission_id, status, site_code, sample_date, reach, net_count, catch_rows, "
                       "total_catch, submitted_by, submitted_login, submitted_at, reviewed_by, reviewed_at, "
                       "review_comment, loaded_harvest_event_id, days_waiting "
                       "FROM entry.vw_submission ORDER BY submission_id DESC")

    def submission(self, sid: int) -> dict:
        r = self._q("SELECT * FROM entry.vw_submission WHERE submission_id = ?", sid)
        if not r:
            raise AppError("Submission not found.", 404)
        s = r[0]
        s["payload"] = json.loads(s["payload"])
        s["log"] = self._q("SELECT [action], action_by, action_login, action_at, comment "
                           "FROM entry.vw_submission_log WHERE submission_id = ? ORDER BY action_at, submission_log_id", sid)
        return s

    def withdraw(self, sid: int, by: str, comment: str | None) -> None:
        self._q("EXEC entry.usp_withdraw_submission @submission_id=?, @by=?, @comment=?", sid, by, comment)

    def review(self, sid: int, decision: str, reviewer: str, comment: str | None) -> dict:
        r = self._q("EXEC entry.usp_review_submission @submission_id=?, @decision=?, @reviewer=?, @comment=?",
                    sid, decision, reviewer, comment)
        return r[0] if r else {}


def make_repo(user: str, password: str):
    return Repo(user, password)


def pack_rows(rows_: list[dict], cols: list[str]) -> dict:
    """Columnar + dictionary-encoded rows: several times smaller than a list of objects."""
    out = {"cols": cols, "data": []}
    for c in cols:
        vals = [r.get(c) for r in rows_]
        uniq: dict = {}
        for v in vals:
            if v is not None and v not in uniq:
                uniq[v] = len(uniq)
        if len(uniq) <= max(50, len(vals) // 3):
            out["data"].append({"dict": list(uniq), "idx": [None if v is None else uniq[v] for v in vals]})
        else:
            out["data"].append({"vals": vals})
    out["n"] = len(rows_)
    return out


# ---------------------------------------------------------------------
# Shared cache: one copy of each dataset / the issue list for everyone,
# served only to sessions whose login may SELECT the underlying view.
# ---------------------------------------------------------------------
class SharedCache:
    def __init__(self):
        self._d: dict[str, tuple[float, object]] = {}
        self._lock = threading.Lock()
        self._building: dict[str, threading.Lock] = {}

    def get_or_build(self, key: str, build, refresh: bool = False):
        if Config.CACHE_SECONDS == 0:
            return build()
        with self._lock:
            hit = self._d.get(key)
            if hit and not refresh and time.time() - hit[0] < Config.CACHE_SECONDS:
                return hit[1]
            lk = self._building.setdefault(key, threading.Lock())
        with lk:  # only one session rebuilds a given entry at a time
            with self._lock:
                hit = self._d.get(key)
                if hit and not refresh and time.time() - hit[0] < Config.CACHE_SECONDS:
                    return hit[1]
            value = build()
            with self._lock:
                self._d[key] = (time.time(), value)
            return value

    def clear(self, prefix: str = "") -> None:
        with self._lock:
            for k in [k for k in self._d if k.startswith(prefix)]:
                self._d.pop(k, None)


cache = SharedCache()


# ---------------------------------------------------------------------
# Sessions + sign-in throttling
# ---------------------------------------------------------------------
_sessions: dict[str, dict] = {}
_lock = threading.Lock()
_failures: dict[str, list[float]] = {}


def _expire_sessions(now: float) -> None:
    for k in [k for k, s in _sessions.items()
              if now - s["last"] > Config.IDLE_SECONDS or now - s["created"] > Config.MAX_SESSION_SECONDS]:
        _sessions.pop(k, None)


def session_or_401() -> dict:
    tok = request.cookies.get(Config.COOKIE)
    now = time.time()
    with _lock:
        _expire_sessions(now)
        s = _sessions.get(tok) if tok else None
        if s:
            s["last"] = now
    if not s:
        raise AppError("Not signed in.", 401)
    g.user = s["user"]
    return s


def session_perms(s: dict) -> dict:
    p = s.get("perms")
    if not p or time.time() - p[0] > 600:
        p = (time.time(), s["repo"].view_permissions())
        s["perms"] = p
    return p[1]


def _fail_keys(user: str) -> list[str]:
    return [f"u:{user.lower()}|{client_ip()}", f"ip:{client_ip()}"]


def _locked_out(user: str) -> int:
    """Seconds left on a lock-out, or 0."""
    now = time.time()
    with _lock:
        for key, limit in zip(_fail_keys(user), (Config.LOGIN_MAX_FAILURES, Config.LOGIN_MAX_FAILURES * 4)):
            hits = [t for t in _failures.get(key, []) if now - t < Config.LOGIN_LOCK_SECONDS]
            _failures[key] = hits
            if len(hits) >= limit:
                return int(Config.LOGIN_LOCK_SECONDS - (now - hits[0])) + 1
    return 0


def _record_failure(user: str) -> None:
    now = time.time()
    with _lock:
        for key in _fail_keys(user):
            _failures.setdefault(key, []).append(now)
        if len(_failures) > 5000:  # keep memory bounded
            for k in list(_failures)[:1000]:
                _failures.pop(k, None)


def _clear_failures(user: str) -> None:
    with _lock:
        _failures.pop(_fail_keys(user)[0], None)


# ---------------------------------------------------------------------
# Input helpers
# ---------------------------------------------------------------------
def body() -> dict:
    b = request.get_json(silent=True)
    return b if isinstance(b, dict) else {}


def clean(v, max_len: int = 4000):
    if v is None:
        return None
    v = str(v).strip()
    if len(v) > max_len:
        raise AppError(f"Text is too long (max {max_len} characters).")
    return v or None


# ---------------------------------------------------------------------
# App
# ---------------------------------------------------------------------
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = Config.MAX_REQUEST_BYTES
app.json.sort_keys = False
if Config.TRUST_PROXY:
    # honour X-Forwarded-Proto/Host/Prefix from Posit Connect or a reverse proxy
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)


def _page() -> str:
    tpl = (SHARED / "hub.html").read_text(encoding="utf-8")
    lib = (SHARED / "xlsx.mini.min.js").read_text(encoding="utf-8")
    return (tpl.replace("/*__XLSX_LIB__*/", lib.replace("</script", "<\\/script"))
               .replace("__APP_VERSION__", APP_VERSION))


PAGE = _page()


@app.before_request
def _guard():
    g.req_id = uuid.uuid4().hex[:8]
    g.user = None
    # CSRF: state-changing API calls must come from our own page (custom header + same origin).
    if request.method == "POST" and request.path.startswith("/api/"):
        if request.headers.get("X-IRBS-Request") != "1":
            raise AppError("Request blocked (missing app header).", 403)
        origin = request.headers.get("Origin")
        if origin and urlparse(origin).netloc != request.host:
            log.warning("blocked cross-origin POST origin=%s host=%s path=%s", origin, request.host, request.path)
            raise AppError("Request blocked (cross-site).", 403)


@app.errorhandler(AppError)
def _app_error(e: AppError):
    return jsonify({"error": str(e)}), e.code


@app.errorhandler(HTTPException)
def _http_error(e: HTTPException):
    msg = {413: "The request is too large.", 404: "Not found.", 405: "Not allowed."}.get(e.code, e.description)
    return jsonify({"error": msg}), e.code


@app.errorhandler(Exception)
def _any_error(e: Exception):
    if is_db_error(e):
        log.warning("db error ref=%s user=%s path=%s: %s", g.get("req_id"), g.get("user"), request.path,
                    str(e).splitlines()[0][:300])
        return jsonify({"error": sql_message(e), "ref": g.get("req_id")}), 502
    log.error("unexpected error ref=%s user=%s path=%s\n%s", g.get("req_id"), g.get("user"), request.path,
              traceback.format_exc())
    return jsonify({"error": f"Something went wrong on the server (reference {g.get('req_id')}). "
                             "Please tell the administrator.", "ref": g.get("req_id")}), 500


CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data: blob:; connect-src 'self'; font-src 'self' data:; object-src 'none'; "
       "base-uri 'none'; form-action 'self'; frame-ancestors 'self'")


@app.after_request
def _headers(resp):
    if (resp.status_code == 200 and not resp.direct_passthrough
            and "gzip" in (request.headers.get("Accept-Encoding") or "")
            and resp.mimetype in ("application/json", "text/html")
            and "Content-Encoding" not in resp.headers):
        data = resp.get_data()
        if len(data) > 2048:
            resp.set_data(gzip.compress(data, compresslevel=5))
            resp.headers["Content-Encoding"] = "gzip"
            resp.headers["Vary"] = "Accept-Encoding"
            resp.headers["Content-Length"] = str(len(resp.get_data()))
    resp.headers["Cache-Control"] = ("public, max-age=86400" if request.path.startswith("/brand/") and resp.status_code == 200
                                     else "no-store")
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "SAMEORIGIN"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    resp.headers["Content-Security-Policy"] = CSP
    if request.is_secure:
        resp.headers["Strict-Transport-Security"] = "max-age=31536000"
    resp.headers["X-Request-Id"] = g.get("req_id", "")
    return resp


# ---- pages / health ----
@app.get("/")
def index():
    return Response(PAGE, mimetype="text/html")


BRAND_DIR = SHARED / "brand"
BRAND_FILES = {"irbs-wide-inverse.png", "irbs-wide-color.png", "illinois-block-i.png"}


BRAND_FONTS = {p.name for p in (BRAND_DIR / "fonts").glob("*.woff2")} if (BRAND_DIR / "fonts").is_dir() else set()


@app.get("/brand/<name>")
def brand(name: str):
    if name not in BRAND_FILES:
        raise AppError("Not found.", 404)
    return send_from_directory(BRAND_DIR, name, max_age=86400)


@app.get("/brand/fonts/<name>")
def brand_font(name: str):
    if name not in BRAND_FONTS:
        raise AppError("Not found.", 404)
    return send_from_directory(BRAND_DIR / "fonts", name, max_age=86400, mimetype="font/woff2")


@app.get("/healthz")
def healthz():
    with _lock:
        n = len(_sessions)
    return jsonify({"status": "ok", "version": APP_VERSION, "uptime_seconds": int(time.time() - STARTED),
                    "active_sessions": n, "database": f"{Config.SERVER} / {Config.DATABASE}"})


@app.get("/api/config")
def config():
    tok = request.cookies.get(Config.COOKIE)
    with _lock:
        _expire_sessions(time.time())
        logged = bool(tok and tok in _sessions)
    return jsonify({"server": Config.SERVER, "database": Config.DATABASE, "logged_in": logged,
                    "version": APP_VERSION})


# ---- sign in / out ----
@app.post("/api/login")
def login():
    b = body()
    user = clean(b.get("user"), 128)
    if not user:
        raise AppError("Enter a username.")
    password = b.get("password") or ""
    if not isinstance(password, str) or len(password) > 256:
        raise AppError("Invalid password.")
    wait = _locked_out(user)
    if wait:
        audit.warning("login LOCKED user=%s ip=%s", user, client_ip())
        raise AppError(f"Too many failed sign-in attempts. Try again in {max(1, wait // 60)} minute(s).", 429)
    repo = make_repo(user, password)
    try:
        repo.test_login()
    except Exception as exc:
        msg = str(exc)
        if "Login failed" in msg or "28000" in msg:
            _record_failure(user)
            audit.warning("login FAILED user=%s ip=%s", user, client_ip())
            raise AppError("Sign-in failed - check the username and password.", 401)
        log.warning("login could not reach database user=%s: %s", user, msg.splitlines()[0][:300])
        raise AppError(f"Could not reach the database: {sql_message(exc)}", 502)
    _clear_failures(user)
    tok = secrets.token_urlsafe(32)
    now = time.time()
    with _lock:
        _expire_sessions(now)
        if len(_sessions) >= Config.MAX_SESSIONS:  # drop the least recently used
            oldest = min(_sessions, key=lambda k: _sessions[k]["last"])
            _sessions.pop(oldest, None)
        _sessions[tok] = {"repo": repo, "user": user, "created": now, "last": now}
    audit.info("login OK user=%s ip=%s", user, client_ip())
    resp = jsonify({"ok": True})
    resp.set_cookie(Config.COOKIE, tok, httponly=True, samesite="Strict", path="/",
                    secure=request.is_secure or Config.FORCE_SECURE_COOKIE, max_age=Config.MAX_SESSION_SECONDS)
    return resp


@app.post("/api/logout")
def logout():
    with _lock:
        s = _sessions.pop(request.cookies.get(Config.COOKIE), None)
    if s:
        audit.info("logout user=%s", s["user"])
    resp = jsonify({"ok": True})
    resp.delete_cookie(Config.COOKIE, path="/")
    return resp


@app.get("/api/me")
def me():
    s = session_or_401()
    info = s["repo"].me()
    info.update({"server": Config.SERVER, "database": Config.DATABASE, "user": s["user"], "version": APP_VERSION})
    return jsonify(info)


# ---- explore ----
@app.get("/api/datasets")
def datasets():
    s = session_or_401()
    perms = session_perms(s)
    counts = cache.get_or_build("counts", s["repo"].dataset_counts, request.args.get("refresh") == "1")
    out = []
    for m in counts:
        m = dict(m)
        if not perms.get(m["key"]) and not m.get("unavailable"):
            m["rows"], m["unavailable"] = None, "Your login does not have access to this dataset."
        out.append(m)
    return jsonify({"datasets": out, "generated_at": time.strftime("%Y-%m-%d %H:%M"),
                    "source": f"{Config.SERVER} / {Config.DATABASE}"})


@app.get("/api/dataset/<key>")
def dataset(key: str):
    s = session_or_401()
    ds = next((d for d in DATASETS if d["key"] == key), None)
    if not ds:
        raise AppError("Unknown dataset.", 404)
    if not session_perms(s).get(key):
        raise AppError("Your login does not have access to this dataset.", 403)
    packed = cache.get_or_build(f"ds:{key}", lambda: pack_dataset(ds, s["repo"].dataset(ds)),
                                request.args.get("refresh") == "1")
    return jsonify(packed)


# ---- data quality ----
@app.get("/api/qa/issues")
def qa_issues():
    s = session_or_401()
    if not session_perms(s).get("__qa"):
        raise AppError("Your login does not have access to the data-quality views.", 403)

    def build():
        t0 = time.time()
        data = s["repo"].qa_issues()
        data["issues"] = pack_rows(data["issues"], Repo.QA_COLS)
        data.update({"reasons": REASONS, "status_labels": STATUS_LABELS,
                     "loaded_at": time.strftime("%H:%M"), "seconds": round(time.time() - t0, 1)})
        return data
    return jsonify(cache.get_or_build("qa", build, request.args.get("refresh") == "1"))


@app.get("/api/qa/issue")
def qa_issue():
    s = session_or_401()
    a = request.args
    row_id, target_id = a.get("row_id"), a.get("target_id")
    if row_id and not row_id.isdigit() or target_id and not target_id.isdigit():
        raise AppError("Invalid issue reference.")
    return jsonify(s["repo"].qa_detail(clean(a.get("key"), 1000) or "", a.get("kind"), row_id,
                                       clean(a.get("target_table"), 150), target_id, clean(a.get("source_id"), 300)))


@app.post("/api/qa/review")
def qa_review():
    s = session_or_401()
    b = body()
    keys = b.get("keys") or []
    if not isinstance(keys, list) or not keys:
        raise AppError("Select at least one issue.")
    if len(keys) > 50000 or any(not isinstance(k, str) or len(k) > 1000 for k in keys):
        raise AppError("Too many or invalid issue keys.")
    if b.get("status") not in STATUS_LABELS:
        raise AppError("Choose a status.")
    by = clean(b.get("by"), 150)
    if not by:
        raise AppError("Enter your name.")
    if b.get("priority") not in (None, "", "HIGH", "MEDIUM", "LOW"):
        raise AppError("Invalid priority.")

    def opt(name, n):  # None = leave unchanged, "" = clear
        v = b.get(name)
        if v is None:
            return None
        v = str(v).strip()
        if len(v) > n:
            raise AppError(f"{name} is too long (max {n} characters).")
        return v
    n = s["repo"].qa_set_review(keys, b["status"], by, opt("priority", 10), opt("assigned_to", 150),
                                opt("decision", 4000), opt("notes", 4000))
    cache.clear("qa")
    audit.info("qa decision user=%s by=%s status=%s issues=%d", s["user"], by, b["status"], n)
    return jsonify({"written": n})


# ---- data entry ----
def _payload(b: dict) -> dict:
    p = b.get("payload")
    if not isinstance(p, dict):
        raise AppError("The form data is missing.")
    if len(p.get("nets") or []) > 200 or len(p.get("catch") or []) > 2000:
        raise AppError("Too many rows in one entry (max 200 nets, 2000 fish-count rows).")
    return p


@app.get("/api/entry/lookups")
def entry_lookups():
    s = session_or_401()
    return jsonify(cache.get_or_build("lookups", s["repo"].lookups))


@app.post("/api/entry/check")
def entry_check():
    s = session_or_401()
    return jsonify({"problems": s["repo"].check(_payload(body()))})


@app.post("/api/entry/submit")
def entry_submit():
    s = session_or_401()
    b = body()
    by = clean(b.get("by"), 150)
    if not by:
        raise AppError("Enter your name.")
    sid = s["repo"].submit(_payload(b), by, clean(b.get("note")))
    audit.info("entry submitted id=%s user=%s by=%s", sid, s["user"], by)
    return jsonify({"submission_id": sid})


@app.get("/api/entry/submissions")
def entry_submissions():
    return jsonify({"submissions": session_or_401()["repo"].submissions()})


@app.get("/api/entry/submission/<int:sid>")
def entry_submission(sid: int):
    s = session_or_401()
    sub = s["repo"].submission(sid)
    if sub["status"] == "PENDING":
        try:
            sub["problems"] = s["repo"].check(sub["payload"], sid)
        except Exception:
            sub["problems"] = []
    return jsonify(sub)


@app.post("/api/entry/submission/<int:sid>/withdraw")
def entry_withdraw(sid: int):
    s = session_or_401()
    b = body()
    s["repo"].withdraw(sid, clean(b.get("by"), 150) or s["user"], clean(b.get("comment")))
    audit.info("entry withdrawn id=%s user=%s", sid, s["user"])
    return jsonify({"ok": True})


@app.post("/api/entry/submission/<int:sid>/review")
def entry_review(sid: int):
    s = session_or_401()
    b = body()
    decision = (b.get("decision") or "").upper()
    if decision not in ("APPROVE", "REJECT"):
        raise AppError("Decision must be APPROVE or REJECT.")
    reviewer = clean(b.get("by"), 150)
    if not reviewer:
        raise AppError("Enter your name.")
    comment = clean(b.get("comment"))
    if decision == "REJECT" and not comment:
        raise AppError("Please give a reason when rejecting.")
    result = s["repo"].review(sid, decision, reviewer, comment)
    if decision == "APPROVE":
        cache.clear("ds:")      # approved data changes the datasets
        cache.clear("counts")
        cache.clear("lookups")
    audit.info("entry %s id=%s user=%s by=%s event=%s", decision.lower() + "d", sid, s["user"], reviewer,
               result.get("harvest_event_id"))
    return jsonify(result)


log.info("IRBS Data Hub %s starting: database=%s/%s idle=%dmin cache=%dmin query_timeout=%ds proxy=%s",
         APP_VERSION, Config.SERVER, Config.DATABASE, Config.IDLE_SECONDS // 60, Config.CACHE_SECONDS // 60,
         Config.QUERY_TIMEOUT, Config.TRUST_PROXY)


if __name__ == "__main__":
    port = _int_env("PORT", 8050, 1, 65535)
    host = os.environ.get("HOST", "127.0.0.1")
    threads = _int_env("IRBS_THREADS", 8, 1, 64)
    print(f"IRBS Data Hub {APP_VERSION} on http://{'localhost' if host in ('127.0.0.1', '0.0.0.0') else host}:{port}"
          f"  (database: {Config.SERVER} / {Config.DATABASE})", flush=True)
    try:
        from waitress import serve
        serve(app, host=host, port=port, threads=threads)
    except ImportError:
        log.warning("waitress not installed - using Flask's development server (not for production)")
        app.run(host=host, port=port, threaded=True)
