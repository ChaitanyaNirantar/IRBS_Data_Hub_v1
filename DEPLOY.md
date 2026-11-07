# IRBS Data Hub: production deployment

Version 1.2.0 (adds the Ask IRBS chatbot). This guide covers taking the app live and keeping it running.

## 1. Database (one time, in SSMS on `Illinois_River_Biological_Station`)

Run the scripts in this order:

| # | Script | What it does |
|---|---|---|
| 1 | `qa_review/sql/40_create_qa_review_layer.sql` | Creates the data-quality views, review tables and procedure (already done) |
| 2 | `qa_review/sql/42_add_review_helpers.sql` | Adds review history and bulk decisions (already done) |
| 3 | `sql/45_create_harvest_analysis_views.sql` | Creates the Explore datasets (already done) |
| 4 | `sql/47_create_data_entry.sql` | Creates the data-entry holding area and the approve/reject procedure |
| 5 | `sql/48_speed_up_data_quality.sql` | Adds indexes (makes the Data quality tab fast) |
| 6 | `sql/49_roles_and_permissions.sql` | **Optional, for later.** Separates who can read, enter and approve using database roles. Until it is run, each login works with the permissions it already has. |

**Accounts (when you adopt script 49):** give each person their own login. Their name then appears in the audit trail and in the decision history. For security, `irbs-posit-write` (the ETL login) should **not** be used in the app.

## 2. Hosting: Posit Connect (recommended)

### Prerequisites (ask the Posit admin)
- [ ] Connect can run **Python APIs (Flask)**. This needs the Enhanced or Advanced edition.
- [ ] **ODBC Driver 18 for SQL Server** (or 17) is installed on the Connect server.
- [ ] Connect can reach `pri-peanuts.prairie.illinois.edu` on port 1433.
- [ ] Connect is served over **HTTPS**.

### Publish
1. Open a terminal in `web/` and run `python make_posit_bundle.py`. This copies `portal_core.py`, `hub.html`, `xlsx.mini.min.js` and the `brand/` folder into `web/`.
2. In Positron (Posit Publisher): **Add Deployment**.
   - Entrypoint: `app.py` (`app:app`).
   - Files to include: `app.py`, `chat.py`, `portal_core.py`, `hub.html`, `xlsx.mini.min.js`, `requirements.txt` and the **`brand/` folder** (IRBS/Illinois logos and fonts).

   Or from the command line:
   `rsconnect deploy api --entrypoint app:app --title "IRBS Data Hub" .`
3. **Deploy Your Project.**

### Content settings in Connect (required)

| Setting | Value | Why |
|---|---|---|
| Runtime › **Max processes** | **1** | Sign-ins are held in memory. With more than one process, users get signed out at random. One process serves many users at once using threads. |
| Runtime › Min processes | 1 | Keeps the app warm, so there's no slow first load. |
| Runtime › Idle timeout | 3600 s or more | Keeps sessions from being dropped while people work. |
| Access | "Specific users or groups" or "All users – login required" | A first gate before the database sign-in. |
| Vars | See section 4 | Only if the defaults don't fit. |

After the first deploy, open `<app URL>/healthz`. It should show `"status": "ok"`.

## 3. Hosting: a Windows server or station PC (alternative)

1. Install Python 3.10+ and ODBC Driver 18 for SQL Server.
2. Copy the Data Hub folder, then run `pip install -r web\requirements.txt`.
3. In `web\start_portal.bat`, set `HOST=0.0.0.0`. Optionally run it as a Windows service, e.g. with NSSM: `nssm install IRBSDataHub python app.py`, with the working directory set to `web\`.
4. **Put it behind HTTPS** (IIS reverse proxy with a campus certificate). If that isn't possible, only allow access from the campus network or VPN.
5. When it's served over HTTPS through a proxy, set `IRBS_SECURE_COOKIES=1`.

## 4. Settings (environment variables)

All are optional. On Connect, set them under **Vars**.

| Variable | Default | Meaning |
|---|---|---|
| `IRBS_SQL_SERVER` | `pri-peanuts.prairie.illinois.edu` | Database server |
| `IRBS_SQL_DATABASE` | `Illinois_River_Biological_Station` | Database |
| `IRBS_SQL_DRIVER` | auto (18 › 17 › …) | Force a specific ODBC driver |
| `IRBS_SQL_TRUST_SERVER_CERTIFICATE` | `yes` | Set to `no` once the SQL Server has a trusted certificate |
| `IRBS_IDLE_MINUTES` | 30 | Signs people out after this much inactivity |
| `IRBS_MAX_SESSION_HOURS` | 12 | Forces a fresh sign-in after this long, even if active |
| `IRBS_MAX_SESSIONS` | 300 | Cap on sessions held in memory |
| `IRBS_QUERY_TIMEOUT` | 300 | Seconds before a database query is cancelled |
| `IRBS_LOGIN_TIMEOUT` | 20 | Seconds to wait when connecting |
| `IRBS_CACHE_MINUTES` | 10 | How long datasets and the issue list are reused (0 = never cache) |
| `IRBS_LOGIN_MAX_FAILURES` | 5 | Failed sign-ins before a lock-out (per user + computer) |
| `IRBS_LOGIN_LOCK_MINUTES` | 15 | Length of the lock-out |
| `IRBS_TRUST_PROXY` | `1` | Trust Connect's or the reverse proxy's forwarded headers |
| `IRBS_SECURE_COOKIES` | `0` | Always mark the cookie Secure (use behind HTTPS) |
| `IRBS_LOG_LEVEL` | `INFO` | `DEBUG` for troubleshooting |
| `IRBS_THREADS` | 8 | Worker threads (Windows/waitress only) |
| `IRBS_OLLAMA_URL` | `http://localhost:11434` | Where Ollama is running (Ask IRBS) |
| `IRBS_CHAT_MODEL` | `qwen2.5-coder:7b` | Ollama model used to write SQL |
| `IRBS_CHAT_TIMEOUT` | 120 | Seconds to wait for the model |
| `IRBS_CHAT_MAX_ROWS` | 500 | Most rows an Ask IRBS answer returns |
| `IRBS_CHAT_QUERY_TIMEOUT` | 60 | Seconds before an Ask IRBS query is cancelled |

## 5. Ask IRBS chatbot (local Ollama)

The **Ask IRBS** tab needs Ollama running somewhere the app can reach. Everything stays on
your network. If Ollama isn't available, the tab says so and the rest of the app works as usual.

### Set up Ollama
1. Install Ollama from <https://ollama.com/download> on the computer that runs the app
   (or on another station machine).
2. In a terminal, run: `ollama pull qwen2.5-coder:7b`. It's a one-time download of about 4.7 GB.
3. Ollama starts automatically (it runs in the system tray on Windows). To check, open
   <http://localhost:11434> in a browser. It should say "Ollama is running".
4. Open the app's **Ask IRBS** tab. The banner should say the model is ready.

### Hardware and model choice
- `qwen2.5-coder:7b` needs about **8 GB of free RAM**. With a recent NVIDIA GPU, answers take
  a few seconds; on CPU only, 20–60 seconds.
- On a lighter machine, use `ollama pull qwen2.5-coder:3b` and set `IRBS_CHAT_MODEL=qwen2.5-coder:3b`.
  It's faster but makes more SQL mistakes.
- A bigger model (e.g. `qwen2.5-coder:14b`) writes better SQL if the hardware allows.

### Where it can run
| App running on | Ollama |
|---|---|
| Your PC / PyCharm | Install Ollama on the same PC. No settings needed. |
| Station Windows server | Install Ollama on that server. No settings needed. |
| **Posit Connect** | Connect usually can't run Ollama. Run Ollama on a station machine, allow its port (11434) from the Connect server, start it with `OLLAMA_HOST=0.0.0.0` and set `IRBS_OLLAMA_URL=http://<that-machine>:11434` in Connect's Vars. Ollama has no password, so only allow the Connect server through the firewall. If that's not possible, Ask IRBS is unavailable on Connect, and the other tabs work as usual. |

### Safety
- **Read-only.** The model can only produce one `SELECT` on the Harvest views the user can read. Writes,
  `EXEC`, system tables and other schemas are refused before anything runs. The query also runs
  inside a transaction that is always rolled back.
- **The user's own login.** The chatbot can never see more than that person already can.
- **Limits.** 60 s query timeout, 500 rows and 15 questions a minute per person.
- **Audited.** `chat ok` / `chat REFUSED` / `chat FAILED` lines in the log record the user, the question and the SQL.
- **Language models make mistakes.** The SQL is always shown. Check it before using a number in a report.

## 6. What's built in

**Security**
- Database passwords are held only in memory for the session. They're never written to disk, logs or cookies.
- The session cookie is HttpOnly and SameSite=Strict, and marked Secure over HTTPS.
- Sign-ins lock out after repeated failures.
- Every change request must come from the app's own page, which blocks cross-site requests.
- Strict browser security headers (Content-Security-Policy, frame and referrer policies, HSTS over HTTPS).
- Request size and input lengths are capped.
- Usernames are quoted in the connection string, so they can't change connection settings.
- CSV downloads are protected against spreadsheet formula injection.
- **Permissions are enforced by SQL Server roles.** The app also checks each login's access before serving cached data.

**Reliability**
- Query timeouts, so a stuck query can't hold a session.
- Expired sessions are cleaned up automatically, and the number held in memory is capped.
- Friendly error messages with a reference code. The full detail goes to the log under that code.

**Performance**
- One shared copy of each dataset and of the issue list, refreshed every 10 minutes and after approvals or decisions.
- Responses are compressed.

**Operations**
- `/healthz` endpoint for monitoring (no sign-in needed, no database touched).
- Audit log lines for sign-ins (OK, failed, locked), decisions, submissions, approvals and rejections.
- Slow-query warnings (over 2 s).
- Version number shown in the footer and on `/healthz`.

## 7. Logs and troubleshooting

On Connect: open the app › **Logs**. On Windows: the console window, or the service log.

| You see | Meaning / fix |
|---|---|
| `login FAILED user=…` | Wrong password. Repeated failures cause a lock-out (`login LOCKED`). |
| `slow query 12.3s … vw_etl_issue_queue` | Run `48_speed_up_data_quality.sql`, or send the line to the developer. |
| A user reports "reference ab12cd34" | Search the log for `ref=ab12cd34` to get the full error. |
| "Could not reach the database" | Network/VPN, server down, or a firewall between Connect and pri-peanuts. |
| "No SQL Server ODBC driver found" | Install ODBC Driver 18 on the host. |
| Users signed out at random | More than one process is running. Set Max processes = 1. |
| Ask IRBS: "Can't reach Ollama" | Ollama isn't running, or `IRBS_OLLAMA_URL` is wrong. Open that URL in a browser. |
| Ask IRBS: "model isn't installed" | Run `ollama pull qwen2.5-coder:7b` (or the model in `IRBS_CHAT_MODEL`). |
| Ask IRBS answers are slow | The model is running on CPU. Use a smaller model or a machine with a GPU. |
| Ask IRBS gives a wrong number | Open **SQL used** and see what it counted. Rephrase more precisely (species code, year, reach). |
| Tab shows "Your login does not have access…" | Add the login to the right role (`49_roles_and_permissions.sql`). |

## 8. Updating the app

1. Replace the changed files. If `hub.html` or `portal_core.py` changed, run `make_posit_bundle.py` again.
2. Run the tests (no database needed): `pip install pytest` then `python -m pytest tests -q`.
3. Redeploy: **Deploy Your Project**, or restart the Windows service.

Connect keeps previous versions, so you can roll back under **Source Versions**.

**Before going live:** test the data-entry approval with one dummy entry. Approve it, check the rows in the Harvest tables, then delete them.
