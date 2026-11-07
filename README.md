# IRBS Data Hub

> **Deploying or running it for the team? See [DEPLOY.md](DEPLOY.md)**: the production checklist, settings, logs and troubleshooting.

A single web app for the Harvest database. Everyone signs in with their SQL
Server login and sees five tabs:

| Tab | What it does | Needs (SQL Server permission) |
|---|---|---|
| **Explore data** | Browse the analysis-ready datasets, filter them, download as Excel or CSV. | SELECT on `irbs.vw_harvest_*` |
| **Data quality** | ETL rejects and quality issues, with a plain-English reason and what to check for each. Open an issue to see the raw Access record. Record decisions one at a time or in bulk. | SELECT on `irbs.vw_etl_*`; EXECUTE on `irbs.usp_set_issue_review` to record decisions |
| **Data entry** | Enter a new sampling event (site, water, nets, fish counts, weights). Check it, then submit it. It's **held for review**. | role `irbs_data_entry` |
| **Ask IRBS** | Ask a question in plain English ("How many fish were sampled in 2021?"). A local model (Ollama) writes the SQL, the app runs it read-only and answers, showing the SQL and the result table. | SELECT on `irbs.vw_harvest_*`; Ollama running (see DEPLOY.md §5) |
| **Review** | Shown only to reviewers. Approve to load into the master Harvest tables, or reject with a reason. | role `irbs_data_reviewer` |

## How Ask IRBS works

1. The question goes to a language model running **on your own computer or server, through
   [Ollama](https://ollama.com)**. Nothing is sent to an outside AI service.
2. The model is given the column lists of the Harvest views (`irbs.vw_harvest_*`) and some
   IRBS rules (e.g. "fish sampled" = `SUM(catch_count)`, how CPUE is calculated). It replies with one SQL query.
3. The app **checks the query before running it**. It must be:
   - one `SELECT`/`WITH` statement;
   - only on the Harvest views your login can read;
   - free of any write, `EXEC`, system table or variable.
   Anything else is refused and not run.
4. The query runs **with your own SQL login**, inside a transaction that is always rolled
   back, with a 60-second timeout and at most 500 rows returned. If SQL Server rejects it,
   the model gets one chance to fix it.
5. You see the answer, what was counted, the **SQL used** (always check it for important
   numbers), the result table and a download button. Follow-up questions ("and in 2022?")
   use the last few questions as context.

Every question and the SQL it produced are written to the audit log.

## How data entry works

1. A team member fills in the form and clicks **Check**. Errors must be fixed
   before submitting; warnings are passed on to the reviewer. An unsent draft is
   kept in their browser.
2. **Submit** stores the entry in `entry.submission` with status *Pending*.
   Nothing touches the `irbs.*` tables yet.
3. The reviewer (Toby) sees it in **Review**, with the automatic checks
   re-run. Then:
   - **Approve & load** runs `entry.usp_review_submission`, which does three things in one transaction:
     - Creates the location, sampling occurrence, harvest event and environment rows.
     - Creates the nets, catch summaries and weights.
     - Records lineage in `irbs.source_record_map` (source `IRBS Data Entry`).
     The entry becomes *Approved* and appears in Explore data.
   - **Reject** requires a comment. The submitter sees it under *Submissions*,
     can **copy it into a new entry**, fix it and resubmit.
4. Every action is logged in `entry.submission_log`, with the person's name
   and their SQL login.

## Setup (once, in SSMS on Illinois_River_Biological_Station)

1. `sql/45_create_harvest_analysis_views.sql`: the datasets for Explore data.
2. `sql/47_create_data_entry.sql`: the holding area, checks, and the approve/reject procedure.
3. QA objects from `qa_review/sql` (40 and 42), which you've already run.
4. Toby runs the permission block at the bottom of 45 and 47. It creates the
   `irbs_data_entry` and `irbs_data_reviewer` roles and adds logins to them.
   Only the reviewer role can approve.

## Run it

```
cd data_portal\web
start_portal.bat            (installs Flask/waitress if needed, opens http://localhost:8050)
```

For the team, run it where it can reach pri-peanuts:

- **Posit Connect (Prairie) — recommended.** You get HTTPS and a stable URL.
  ```
  python make_posit_bundle.py
  rsconnect deploy api --entrypoint app:app --title "IRBS Data Hub" .
  ```
- **A station PC.** In `start_portal.bat`, set `HOST=0.0.0.0`. Use it only on the
  campus network or VPN, because without HTTPS passwords travel unencrypted.

Passwords are kept only in the app's memory, for that browser session. They're
dropped on sign-out or after 30 idle minutes (`IRBS_IDLE_MINUTES`).

## Files

| File | |
|---|---|
| `hub.html` | The app's page (all four tabs). |
| `web/app.py` | The web server and API. |
| `portal_core.py` | Shared code: dataset definitions, column descriptions, issue-code reasons, database connection. |
| `xlsx.mini.min.js` | Excel export library (SheetJS, Apache-2.0). |
| `brand/` | IRBS design-system assets: IRBS and Illinois logos, plus the Montserrat, Source Sans 3 and Atkinson Hyperlegible fonts (SIL OFL). |
| `sql/45_…` – `sql/49_…` | Database objects: views, data entry, indexes, roles and permissions. |
| `tests/` | Automated checks of the web app (no database needed): `python -m pytest tests -q`. |
| `DEPLOY.md` | Production deployment guide. |
| `46_build_data_portal.py` + `portal_template.html` | Optional: build a one-file offline snapshot of the datasets to send to people who have no login. |

## Known limits

- Data entry covers Harvest sampling events: site, water, nets, fish counts and
  weights. Individual fish and tag entry aren't included yet.
- Catches entered without a net are stored at site level (`catch_level = EVENT`).
- VR100 detections aren't migrated yet, so there's no dataset for them.
