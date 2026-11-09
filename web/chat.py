"""
"Ask IRBS" - questions in plain English about the Harvest data, answered by a
LOCAL Ollama model that writes a read-only SQL query against the analysis views.

Flow:  question -> Ollama writes T-SQL -> validate_sql() -> run with the user's
       own login (timeout + row cap) -> Ollama phrases the answer from the rows.

Safety: validate_sql() only lets through ONE SELECT/WITH statement that reads
ONLY the allow-listed irbs.vw_harvest_* views. No DML/DDL, EXEC, temp tables,
system objects, linked servers or variables. The query also runs under the
signed-in user's SQL permissions, so the database is the final guard.

Settings (environment variables):
  IRBS_OLLAMA_URL        default http://localhost:11434
  IRBS_CHAT_MODEL        default qwen2.5-coder:7b   (any Ollama model you have pulled)
  IRBS_CHAT_TIMEOUT      seconds to wait for the model, default 120
  IRBS_CHAT_MAX_ROWS     rows returned to the browser, default 500
  IRBS_CHAT_QUERY_TIMEOUT  seconds a chatbot query may run, default 60
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

OLLAMA_URL = os.environ.get("IRBS_OLLAMA_URL", "http://localhost:11434").rstrip("/")
CHAT_MODEL = os.environ.get("IRBS_CHAT_MODEL", "qwen2.5-coder:7b")
CHAT_TIMEOUT = int(os.environ.get("IRBS_CHAT_TIMEOUT", "120"))
MAX_ROWS = int(os.environ.get("IRBS_CHAT_MAX_ROWS", "500"))
QUERY_TIMEOUT = int(os.environ.get("IRBS_CHAT_QUERY_TIMEOUT", "60"))


class ChatError(Exception):
    pass


# ---------------------------------------------------------------------
# SQL validation (the important part)
# ---------------------------------------------------------------------
FORBIDDEN = re.compile(
    r"\b(insert|update|delete|merge|drop|alter|create|truncate|exec|execute|grant|revoke|deny|"
    r"into|openrowset|openquery|opendatasource|openxml|bulk|waitfor|shutdown|dbcc|backup|restore|"
    r"declare|set|use|kill|reconfigure|sp_\w*|xp_\w*|fn_\w*|sys|information_schema|"
    r"readtext|writetext|updatetext|load|dump|go)\b", re.I)


def _strip_comments_and_strings(sql: str) -> tuple[str, str]:
    """Returns (sql without comments, same with string literals blanked) for keyword checks."""
    no_comments = re.sub(r"--[^\n]*", " ", sql)
    no_comments = re.sub(r"/\*.*?\*/", " ", no_comments, flags=re.S)
    blanked = re.sub(r"N?'(?:[^']|'')*'", "''", no_comments)
    return no_comments, blanked


def validate_sql(sql: str, allowed_views: set[str]) -> str:
    """Return the cleaned SQL or raise ChatError explaining why it was refused."""
    if not sql or not sql.strip():
        raise ChatError("The model did not produce a query.")
    sql = sql.strip().strip("`")
    sql = re.sub(r"^\s*sql\s*\n", "", sql, flags=re.I)  # stray ```sql fence label
    cleaned, blanked = _strip_comments_and_strings(sql)
    cleaned = cleaned.strip().rstrip(";").strip()
    blanked = blanked.strip().rstrip(";").strip()
    if len(cleaned) > 8000:
        raise ChatError("The generated query is too long.")
    if ";" in blanked:
        raise ChatError("Only a single statement is allowed.")
    if not re.match(r"^(select|with)\b", blanked, re.I):
        raise ChatError("Only SELECT queries are allowed.")
    bad = FORBIDDEN.search(blanked)
    if bad:
        raise ChatError(f"The query uses '{bad.group(0)}', which is not allowed here.")
    if re.search(r"[#@]", blanked):
        raise ChatError("Temporary tables and variables are not allowed.")
    if re.search(r"\b\w+\s*\.\s*\w+\s*\.\s*\w+\s*\.\s*\w+\b|\[[^\]]+\]\s*\.\s*\[[^\]]+\]\s*\.\s*\[", blanked):
        raise ChatError("Cross-database or linked-server references are not allowed.")

    allowed = {v.lower() for v in allowed_views}
    norm = re.sub(r"[\[\]]", "", blanked)
    # every schema.object reference must be an allowed view
    cte_names = {m.lower() for m in re.findall(r"(?:\bwith|,)\s*(\w+)\s*(?:\([^)]*\))?\s+as\s*\(", norm, re.I)}
    for schema, obj in re.findall(r"\b(\w+)\s*\.\s*(\w+)\b", norm):
        full = f"{schema}.{obj}".lower()
        if schema.lower() in ("irbs", "dbo", "entry", "raw_ac") or full.startswith(("irbs.", "dbo.")):
            if full not in allowed:
                raise ChatError(f"The query reads {schema}.{obj}, which the chatbot may not use.")
    # FROM / JOIN targets must be allowed views or CTEs defined in this query
    for target in re.findall(r"\b(?:from|join|apply)\s+([\w.]+)", norm, re.I):
        t = target.lower()
        if t.startswith("("):
            continue
        if t in allowed or t in cte_names:
            continue
        if "." not in t and t in ("(", "select"):
            continue
        raise ChatError(f"The query reads '{target}', which is not one of the Harvest views.")
    if not any(v in norm.lower() for v in allowed):
        raise ChatError("The query does not use any Harvest view.")
    return cleaned


# ---------------------------------------------------------------------
# Ollama client
# ---------------------------------------------------------------------
def _post(path: str, payload: dict, timeout: int) -> dict:
    req = urllib.request.Request(OLLAMA_URL + path, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")[:300]
        if e.code == 404 and "model" in body.lower():
            raise ChatError(f"The model '{CHAT_MODEL}' is not installed in Ollama. Run:  ollama pull {CHAT_MODEL}")
        raise ChatError(f"Ollama returned an error ({e.code}): {body}")
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise ChatError(f"Can't reach Ollama at {OLLAMA_URL}. Is it running? ({getattr(e, 'reason', e)})")


def ollama_status() -> dict:
    try:
        req = urllib.request.Request(OLLAMA_URL + "/api/tags")
        with urllib.request.urlopen(req, timeout=15) as r:
            tags = json.loads(r.read().decode("utf-8"))
        names = [m.get("name", "") for m in tags.get("models", [])]
        installed = any(n == CHAT_MODEL or n.split(":")[0] == CHAT_MODEL.split(":")[0] and
                        (":" not in CHAT_MODEL or n == CHAT_MODEL) for n in names)
        return {"reachable": True, "model": CHAT_MODEL, "installed": installed, "models": names[:20],
                "url": OLLAMA_URL}
    except Exception as e:  # noqa: BLE001
        return {"reachable": False, "model": CHAT_MODEL, "installed": False, "url": OLLAMA_URL,
                "error": str(getattr(e, "reason", e))[:200]}


def chat_completion(messages: list[dict], json_mode: bool = False, temperature: float = 0.0) -> str:
    payload = {"model": CHAT_MODEL, "messages": messages, "stream": False,
               "options": {"temperature": temperature, "num_ctx": 8192}}
    if json_mode:
        payload["format"] = "json"
    out = _post("/api/chat", payload, CHAT_TIMEOUT)
    return (out.get("message") or {}).get("content", "")


# ---------------------------------------------------------------------
# Prompting
# ---------------------------------------------------------------------
DOMAIN_NOTES = """Domain notes (IRBS Harvest program, commercial-fisher removal of invasive carp, Illinois River):
- "How many fish were sampled / caught / collected / counted" = SUM(catch_count) from irbs.vw_harvest_catch.
  Do NOT use COUNT(*) for number of fish: each row is one species at one net or site with a catch_count.
- Number of sampling trips/events/sites visited = COUNT(DISTINCT harvest_event_id).
- Number of nets set = COUNT(*) from irbs.vw_harvest_net_effort. Effort in yards = SUM(yardage).
- CPUE (catch per 100 yards of net) = 100.0 * SUM(catch_count) / total yardage, where each net's yardage is
  counted once: use irbs.vw_harvest_net_effort for the yardage and irbs.vw_harvest_catch_per_net for the catch.
- Weight (kg) of carp: irbs.vw_harvest_weights_by_site (silver_kg, bighead_kg, grass_kg, hybrid_kg, unsorted_kg, total_kg).
- Individually measured/tagged fish (lengths, weights, sex, ages): irbs.vw_harvest_individual_fish.
- Tags: irbs.vw_harvest_tag_deployments (tags put on fish), irbs.vw_harvest_tag_recaptures (recaptures).
- Year: use the [year] column (or YEAR(sample_date)). Months: [month] 1-12.
- Common species codes: SVCP = silver carp, BHCP = bighead carp, GSCP = grass carp, CARP = common carp,
  BKBF = black buffalo, BMBF = bigmouth buffalo, SMBF = smallmouth buffalo, FWDM = freshwater drum,
  PDFH = paddlefish, CNCF = channel catfish, FHCF = flathead catfish, BLCF = blue catfish.
  Match species by species_code, or by species_name LIKE '%silver carp%' when the user gives a name.
- Data in this database covers roughly 2015-2022."""

SQL_RULES = """Rules for the SQL you write:
- Microsoft SQL Server (T-SQL). Use TOP (n), never LIMIT. Use [year] and [month] in square brackets.
- ONE read-only SELECT statement (a WITH ... SELECT is fine). No variables, no temp tables, no semicolons.
- Use ONLY the views listed below, always with the schema: irbs.vw_harvest_...
- Give result columns clear names with AS (e.g. AS fish_counted).
- For lists, ORDER BY something sensible and use TOP (100) unless the user asks for everything.
- If the question cannot be answered from these views, return sql = "" and explain why."""


def build_schema_text(columns: dict[str, list[tuple[str, str]]], descriptions: dict[str, str],
                      col_desc: dict[str, str]) -> str:
    parts = []
    for view, cols in columns.items():
        lines = [f"{view} -- {descriptions.get(view, '')}"]
        for name, typ in cols:
            d = col_desc.get(name)
            lines.append(f"  {name} {typ}" + (f"  -- {d}" if d else ""))
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def sql_messages(question: str, schema_text: str, history: list[dict] | None = None,
                 previous_error: str | None = None, previous_sql: str | None = None) -> list[dict]:
    system = (
        "You translate questions about the Illinois River Biological Station (IRBS) Harvest database into "
        "a single read-only T-SQL query.\n\n" + SQL_RULES + "\n\n" + DOMAIN_NOTES + "\n\nAvailable views:\n\n"
        + schema_text + "\n\nReply with JSON only: {\"sql\": \"...\", \"interpretation\": \"one sentence saying "
        "exactly what you counted/measured, from which view\"}")
    msgs = [{"role": "system", "content": system}]
    for h in (history or [])[-4:]:
        if h.get("question") and h.get("sql"):
            msgs.append({"role": "user", "content": h["question"]})
            msgs.append({"role": "assistant", "content": json.dumps({"sql": h["sql"], "interpretation": h.get("interpretation", "")})})
    msgs.append({"role": "user", "content": question})
    if previous_error:
        msgs.append({"role": "assistant", "content": json.dumps({"sql": previous_sql or "", "interpretation": ""})})
        msgs.append({"role": "user", "content": f"That query failed with: {previous_error}\nFix it and reply with JSON only."})
    return msgs


def parse_sql_reply(text: str) -> tuple[str, str]:
    text = (text or "").strip()
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        obj = None
        if m:
            try:
                obj = json.loads(m.group(0))
            except json.JSONDecodeError:
                obj = None
        if obj is None:  # model ignored JSON mode: take a ```sql block or the raw text
            m = re.search(r"```(?:sql)?\s*(.*?)```", text, re.S | re.I)
            return (m.group(1) if m else text).strip(), ""
    return str(obj.get("sql") or "").strip(), str(obj.get("interpretation") or "").strip()


def answer_messages(question: str, interpretation: str, sql: str, columns: list[str], rows: list[list],
                    truncated: bool) -> list[dict]:
    sample = rows[:40]
    table = json.dumps({"columns": columns, "rows": sample}, default=str)
    return [
        {"role": "system", "content":
            "You answer questions about IRBS Harvest data in 1-3 short, plain sentences for biologists. "
            "Use ONLY the numbers in the query result; never invent or estimate numbers. Format large numbers "
            "with thousands separators. Say what was counted (from the interpretation). If the result is empty, "
            "say no matching records were found. Do not mention SQL."},
        {"role": "user", "content":
            f"Question: {question}\nWhat was counted: {interpretation}\nResult ({len(rows)} rows"
            f"{', more not shown' if truncated or len(rows) > len(sample) else ''}): {table}"},
    ]


def simple_answer(columns: list[str], rows: list[list]) -> str | None:
    """Deterministic answer for a single-value result (no model call needed)."""
    if len(rows) == 1 and len(columns) == 1:
        v = rows[0][0]
        if isinstance(v, (int, float)):
            v = f"{v:,.2f}".rstrip("0").rstrip(".") if isinstance(v, float) else f"{v:,}"
        return f"{columns[0].replace('_', ' ').capitalize()}: {v if v is not None else 'no data'}"
    if not rows:
        return "No matching records were found."
    return None
