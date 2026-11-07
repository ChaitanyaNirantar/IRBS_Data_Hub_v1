"""Tests for the Ask IRBS chatbot: the SQL safety check and the /api/chat flow (fake model + fake DB)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(ROOT))

import chat  # noqa: E402

VIEWS = {"irbs.vw_harvest_catch", "irbs.vw_harvest_net_effort", "irbs.vw_harvest_catch_per_net",
         "irbs.vw_harvest_individual_fish"}

GOOD = [
    "SELECT SUM(catch_count) AS fish_counted FROM irbs.vw_harvest_catch WHERE [year] = 2021",
    "select sum(catch_count) as fish from irbs.vw_harvest_catch where year = 2021;",
    "SELECT TOP (10) species_code, SUM(catch_count) AS n FROM [irbs].[vw_harvest_catch] c GROUP BY species_code ORDER BY n DESC",
    """WITH nets AS (SELECT [year], SUM(yardage) AS yards FROM irbs.vw_harvest_net_effort GROUP BY [year]),
       fish AS (SELECT [year], SUM(catch_count) AS fish FROM irbs.vw_harvest_catch_per_net
                WHERE species_code = 'SVCP' GROUP BY [year])
       SELECT f.[year], 100.0 * f.fish / NULLIF(n.yards, 0) AS cpue FROM fish f JOIN nets n ON n.[year] = f.[year]""",
    "SELECT COUNT(DISTINCT harvest_event_id) AS events FROM irbs.vw_harvest_catch WHERE site_code LIKE '%drop table%'",
    "SELECT AVG(length_mm) AS mean_len FROM irbs.vw_harvest_individual_fish -- comment with delete\n WHERE species_code='SVCP'",
    "SELECT set_type, COUNT(*) AS nets FROM irbs.vw_harvest_net_effort GROUP BY set_type",
    "SELECT * FROM (SELECT [year], catch_count FROM irbs.vw_harvest_catch) x WHERE x.[year] > 2018",
]

BAD = [
    ("DELETE FROM irbs.vw_harvest_catch", "SELECT"),
    ("SELECT 1; DROP TABLE irbs.harvest_event", "single statement"),
    ("SELECT * FROM irbs.harvest_event", "may not use"),
    ("SELECT * FROM irbs.etl_reject", "may not use"),
    ("SELECT * FROM sys.tables", "sys"),
    ("SELECT * FROM INFORMATION_SCHEMA.TABLES", "information_schema"),
    ("SELECT * INTO #t FROM irbs.vw_harvest_catch", "into"),
    ("SELECT * FROM irbs.vw_harvest_catch; EXEC xp_cmdshell 'dir'", "single statement"),
    ("EXEC sp_who", "SELECT"),
    ("SELECT * FROM OPENROWSET('SQLNCLI','x','select 1')", "openrowset"),
    ("SELECT * FROM otherdb.irbs.vw_harvest_catch", "not one of"),
    ("SELECT * FROM srv.db.irbs.vw_harvest_catch", "linked-server"),
    ("WITH x AS (SELECT 1 AS a) UPDATE irbs.vw_harvest_catch SET catch_count = 0", "update"),
    ("SELECT name FROM entry.submission", "may not use"),
    ("SELECT @@VERSION", "variables"),
    ("SELECT 1 WAITFOR DELAY '00:01'", "waitfor"),
    ("SELECT * FROM dbo.users", "may not use"),
    ("SELECT 1 AS x", "does not use any"),
    ("", "did not produce"),
]


@pytest.mark.parametrize("sql", GOOD)
def test_allowed_queries_pass(sql):
    assert chat.validate_sql(sql, VIEWS)


@pytest.mark.parametrize("sql,why", BAD)
def test_dangerous_or_out_of_scope_queries_are_refused(sql, why):
    with pytest.raises(chat.ChatError) as e:
        chat.validate_sql(sql, VIEWS)
    assert why.lower() in str(e.value).lower()


def test_parse_reply_variants():
    assert chat.parse_sql_reply('{"sql": "SELECT 1", "interpretation": "x"}') == ("SELECT 1", "x")
    assert chat.parse_sql_reply('Sure!\n```sql\nSELECT 2\n```')[0] == "SELECT 2"
    assert chat.parse_sql_reply('noise {"sql":"SELECT 3","interpretation":""} tail')[0] == "SELECT 3"


def test_simple_answer():
    assert chat.simple_answer(["fish_counted"], [[23456]]) == "Fish counted: 23,456"
    assert chat.simple_answer(["a", "b"], []) == "No matching records were found."
    assert chat.simple_answer(["a", "b"], [[1, 2]]) is None


# ---------------- endpoint flow ----------------
HDR = {"X-IRBS-Request": "1"}


class FakeDbError(Exception):
    """Stands in for pyodbc.Error (the ODBC library isn't installed on the test machine)."""


@pytest.fixture()
def A(monkeypatch):
    import importlib
    import app as A
    importlib.reload(A)
    from test_app import FakeRepo

    class ChatRepo(FakeRepo):
        def chat_schema(self, views):
            return {"irbs.vw_harvest_catch": [("year", "int"), ("catch_count", "int"), ("species_code", "nvarchar")]}

        def run_readonly(self, sql, max_rows, timeout):
            if "BROKEN" in sql:
                raise FakeDbError("[42S22] [Microsoft][ODBC][SQL Server]Invalid column name 'BROKEN'. (207)")
            return ["fish_counted"], [[18542]], False

    A.make_repo = ChatRepo
    monkeypatch.setattr(A, "is_db_error", lambda e: isinstance(e, FakeDbError))
    A._sessions.clear(); A._failures.clear(); A.cache.clear()
    A.app.testing = True
    return A


def _login(c):
    assert c.post("/api/login", json={"user": "reader", "password": "pw"}, headers=HDR).status_code == 200


def test_chat_answers_with_sql(A, monkeypatch):
    calls = []

    def fake_llm(messages, json_mode=False, temperature=0.0):
        calls.append(messages)
        return json.dumps({"sql": "SELECT SUM(catch_count) AS fish_counted FROM irbs.vw_harvest_catch WHERE [year] = 2021",
                           "interpretation": "Total fish counted in 2021 from vw_harvest_catch."})
    monkeypatch.setattr(A.chat, "chat_completion", fake_llm)
    c = A.app.test_client(); _login(c)
    r = c.post("/api/chat", json={"question": "How many fish were sampled in 2021?"}, headers=HDR)
    assert r.status_code == 200, r.json
    assert r.json["answer"] == "Fish counted: 18,542"
    assert "vw_harvest_catch" in r.json["sql"]
    assert "irbs.vw_harvest_catch" in calls[0][0]["content"]       # schema was given to the model


def test_chat_refuses_unsafe_sql_without_running(A, monkeypatch):
    monkeypatch.setattr(A.chat, "chat_completion",
                        lambda *a, **k: json.dumps({"sql": "DELETE FROM irbs.vw_harvest_catch", "interpretation": ""}))
    c = A.app.test_client(); _login(c)
    r = c.post("/api/chat", json={"question": "delete everything"}, headers=HDR)
    assert r.status_code == 200 and r.json.get("refused") and r.json["rows"] == []


def test_chat_retries_once_after_sql_error(A, monkeypatch):
    replies = iter([
        json.dumps({"sql": "SELECT BROKEN FROM irbs.vw_harvest_catch", "interpretation": ""}),
        json.dumps({"sql": "SELECT SUM(catch_count) AS fish_counted FROM irbs.vw_harvest_catch", "interpretation": "ok"}),
    ])
    monkeypatch.setattr(A.chat, "chat_completion", lambda *a, **k: next(replies))
    c = A.app.test_client(); _login(c)
    r = c.post("/api/chat", json={"question": "fish?"}, headers=HDR)
    assert r.status_code == 200 and r.json["rows"] == [[18542]]


def test_chat_reports_ollama_down(A, monkeypatch):
    def down(*a, **k):
        raise A.chat.ChatError("Can't reach Ollama at http://localhost:11434. Is it running?")
    monkeypatch.setattr(A.chat, "chat_completion", down)
    c = A.app.test_client(); _login(c)
    r = c.post("/api/chat", json={"question": "fish?"}, headers=HDR)
    assert r.status_code == 503 and "Ollama" in r.json["error"]


def test_chat_requires_sign_in(A):
    r = A.app.test_client().post("/api/chat", json={"question": "x"}, headers=HDR)
    assert r.status_code == 401
