"""
Automated checks for the IRBS Data Hub web app (no database needed).

    pip install -r web/requirements.txt pytest
    python -m pytest tests -q

A fake database layer stands in for SQL Server, so these test the web layer:
sign-in, lock-out, security headers, permissions, caching, input limits.
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(ROOT))

os.environ.setdefault("IRBS_LOGIN_MAX_FAILURES", "3")
os.environ.setdefault("IRBS_TRUST_PROXY", "0")

HDR = {"X-IRBS-Request": "1"}
CALLS = {"dataset": 0, "qa": 0}


class FakeRepo:
    PASSWORDS = {"reader": "pw", "toby": "pw", "noaccess": "pw"}

    def __init__(self, user, password):
        self.user, self.password = user, password

    def test_login(self):
        if self.PASSWORDS.get(self.user) != self.password:
            raise Exception("('28000', \"[28000] Login failed for user. (18456)\")")

    def me(self):
        return {"login": self.user, "can_review": int(self.user == "toby"), "can_submit": 1,
                "can_qa_review": 1, "can_qa_view": 1, "can_entry_view": 1, "pending": 0}

    def view_permissions(self):
        import app as A
        ok = self.user != "noaccess"
        p = {d["key"]: ok for d in A.DATASETS}
        p["__qa"] = ok
        return p

    def dataset_counts(self):
        import app as A
        return [{"key": d["key"], "view": d["view"], "title": d["title"], "replaces": "", "description": "",
                 "notes": "", "rows": 2} for d in A.DATASETS]

    def dataset(self, ds):
        CALLS["dataset"] += 1
        return pd.DataFrame({"year": [2020, 2021], "catch_count": [3, 4], "site_code": ["A", "B"]})

    def qa_issues(self):
        CALLS["qa"] += 1
        return {"issues": [{"issue_key": "REJECT|x|t|1|C", "issue_kind": "REJECT", "issue_row_id": 1}], "recon": []}

    def qa_set_review(self, keys, *a):
        return len(keys)

    def lookups(self):
        return {"species": [], "gear": [], "reach": []}

    def check(self, payload, sid=None):
        return []

    def submit(self, payload, by, note):
        return 7

    def submissions(self):
        return []

    def review(self, sid, decision, by, comment):
        return {"submission_id": sid, "status": decision + "D", "harvest_event_id": 1}

    def boom(self):
        raise RuntimeError("secret internal detail")


@pytest.fixture()
def A():
    import app as A
    importlib.reload(A)
    A.make_repo = FakeRepo
    A._sessions.clear(); A._failures.clear(); A.cache.clear()
    CALLS.update(dataset=0, qa=0)
    A.app.testing = True
    return A


def login(client, user="reader", pw="pw"):
    return client.post("/api/login", json={"user": user, "password": pw}, headers=HDR)


def test_page_and_security_headers(A):
    c = A.app.test_client()
    r = c.get("/", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200
    assert r.headers["Content-Encoding"] == "gzip"
    for h in ("Content-Security-Policy", "X-Frame-Options", "X-Content-Type-Options", "Referrer-Policy"):
        assert h in r.headers


def test_healthz(A):
    r = A.app.test_client().get("/healthz")
    assert r.status_code == 200 and r.json["status"] == "ok" and r.json["version"] == A.APP_VERSION


def test_post_without_app_header_is_blocked(A):
    r = A.app.test_client().post("/api/login", json={"user": "reader", "password": "pw"})
    assert r.status_code == 403


def test_cross_origin_post_is_blocked(A):
    r = A.app.test_client().post("/api/login", json={"user": "reader", "password": "pw"},
                                 headers={**HDR, "Origin": "https://evil.example"})
    assert r.status_code == 403


def test_login_cookie_flags_and_session(A):
    c = A.app.test_client()
    r = login(c)
    assert r.status_code == 200
    cookie = r.headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
    assert c.get("/api/me").json["login"] == "reader"


def test_wrong_password_then_lockout(A):
    c = A.app.test_client()
    for _ in range(3):
        assert login(c, pw="nope").status_code == 401
    r = login(c)  # correct password, but locked
    assert r.status_code == 429 and "Too many" in r.json["error"]


def test_logout_ends_session(A):
    c = A.app.test_client()
    login(c)
    assert c.post("/api/logout", headers=HDR).status_code == 200
    assert c.get("/api/me").status_code == 401


def test_idle_sessions_expire(A):
    c = A.app.test_client()
    login(c)
    for s in A._sessions.values():
        s["last"] -= A.Config.IDLE_SECONDS + 1
    assert c.get("/api/me").status_code == 401


def test_dataset_permission_and_shared_cache(A):
    c1, c2, c3 = A.app.test_client(), A.app.test_client(), A.app.test_client()
    login(c1); login(c2, "toby"); login(c3, "noaccess")
    assert c1.get("/api/dataset/catch").status_code == 200
    assert c2.get("/api/dataset/catch").status_code == 200
    assert CALLS["dataset"] == 1                      # second user served from the shared cache
    assert c3.get("/api/dataset/catch").status_code == 403
    assert c3.get("/api/qa/issues").status_code == 403
    assert c1.get("/api/dataset/catch?refresh=1").status_code == 200 and CALLS["dataset"] == 2


def test_qa_review_validation_and_cache_clear(A):
    c = A.app.test_client()
    login(c)
    c.get("/api/qa/issues"); c.get("/api/qa/issues")
    assert CALLS["qa"] == 1
    bad = c.post("/api/qa/review", json={"keys": ["k"], "status": "NOPE", "by": "me"}, headers=HDR)
    assert bad.status_code == 400
    many = c.post("/api/qa/review", json={"keys": ["k"] * 50001, "status": "OPEN", "by": "me"}, headers=HDR)
    assert many.status_code == 400
    ok = c.post("/api/qa/review", json={"keys": ["k"], "status": "RESOLVED", "by": "me"}, headers=HDR)
    assert ok.status_code == 200 and ok.json["written"] == 1
    c.get("/api/qa/issues")
    assert CALLS["qa"] == 2                           # decision cleared the cache


def test_entry_limits(A):
    c = A.app.test_client()
    login(c)
    r = c.post("/api/entry/submit", json={"by": "me", "payload": {"site": {}, "nets": [{}] * 201}}, headers=HDR)
    assert r.status_code == 400
    r = c.post("/api/entry/submit", json={"by": "me", "payload": {"site": {"site_code": "X"}, "nets": [], "catch": []}},
               headers=HDR)
    assert r.status_code == 200 and r.json["submission_id"] == 7


def test_request_size_limit(A):
    c = A.app.test_client()
    login(c)
    r = c.post("/api/entry/check", data="x" * (3 * 1024 * 1024), headers={**HDR, "Content-Type": "application/json"})
    assert r.status_code == 413


def test_unexpected_error_hides_details(A):
    c = A.app.test_client()
    login(c)
    A.Repo.QA_COLS  # noqa
    orig = FakeRepo.lookups
    FakeRepo.lookups = FakeRepo.boom
    try:
        r = c.get("/api/entry/lookups")
    finally:
        FakeRepo.lookups = orig
    assert r.status_code == 500
    assert "secret internal detail" not in r.get_data(as_text=True)
    assert r.json["ref"]
