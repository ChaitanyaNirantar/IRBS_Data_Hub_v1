"""
Shared pieces for the IRBS Harvest Data Portal:
dataset definitions, SQL Server connection, and data packing.
Used by 46_build_data_portal.py (static HTML file) and web/app.py (live app).
"""

from __future__ import annotations

import getpass
import os
import sys
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
DEFAULT_SERVER = "pri-peanuts.prairie.illinois.edu"
DEFAULT_DATABASE = "Illinois_River_Biological_Station"

# Shared column descriptions (used by every dataset that has the column)
COMMON_COLS = {
    "harvest_event_id": "Database ID of the sampling event (one Site Data record).",
    "site_code": "AC Master Site_Code (date + site + crew suffix).",
    "sample_date": "Sampling date.",
    "year": "Sampling year.",
    "month": "Sampling month (1-12).",
    "day": "Day of month.",
    "sample_datetime": "Sampling date and time (time may be date-only; see time_basis).",
    "time_basis": "How the sampling time was recorded (exact time vs date only).",
    "reach": "AC Master Reach number.",
    "site_name": "Site name from Site Data.",
    "latitude": "Latitude recorded for the site (decimal degrees).",
    "longitude": "Longitude recorded for the site (decimal degrees).",
    "site_description": "Free-text site description.",
    "depth": "Water depth recorded at the site (Site Data.Depth).",
    "secchi_cm": "Secchi depth (cm).",
    "water_temp_c": "Water temperature (°C).",
    "dissolved_oxygen": "Dissolved oxygen (see dissolved_oxygen_unit).",
    "conductivity": "Specific conductivity (see conductivity_unit).",
    "species_code": "AC Master species code (e.g. SVCP silver carp, BHCP bighead carp).",
    "species_name": "Common name from the species reference table (blank if not mapped yet).",
    "net_number_code": "Net identifier from Net Data (Site_Code + net number).",
    "gear_type": "Gear type code from Net Data.",
    "yardage": "Net length in yards.",
    "soak_time_hours": "Hours the net was in the water.",
    "mesh_size": "Mesh size.",
    "removed": "1 = fish were removed (harvested), 0 = released, blank = unknown.",
    "removed_source_value": "Original Removed value from Access.",
    "source_record_id": "Access row ID the record came from (for tracing back).",
}

# ---------------------------------------------------------------------
# Data-quality issue codes: plain-English reason + what to check
# ---------------------------------------------------------------------
REASONS: dict[str, dict[str, str]] = {
    # ---------- rejects ----------
    "NO_EVENT_PARENT": {
        "title": "No matching Harvest event",
        "meaning": "The row's Site_Code does not match any loaded Site Data event "
                   "(or the row has no Site_Code at all), so it cannot be attached "
                   "to a sampling event. The row was not loaded.",
        "check": "Look the Site_Code up in Access Site Data. Typical causes: a typo or "
                 "wrong date in the code, a Site Data row that was never entered, or a "
                 "blank Site_Code. Fix it in Access, then rerun the loader.",
    },
    "AMBIGUOUS_EVENT_PARENT": {
        "title": "Site_Code matches more than one event",
        "meaning": "The Site_Code belongs to two or more Site Data rows that have "
                   "different contents, so the ETL cannot tell which event this row "
                   "belongs to and refuses to guess.",
        "check": "Compare the duplicate Site Data rows (e.g. 08302019.2A). Decide which "
                 "one is right, or give one of them a distinct Site_Code.",
    },
    "ORPHAN_SITE_CODE": {
        "title": "Net row with no Site Data parent",
        "meaning": "The Net Data row's Site_Code does not exist in Site Data.",
        "check": "Find the site record in Access. If it is missing, add it; if the "
                 "code is mistyped, correct the Net Data row.",
    },
    "AMBIGUOUS_SITE_CODE": {
        "title": "Net row under a duplicated Site_Code",
        "meaning": "The Net Data row's Site_Code matches several differing Site Data "
                   "rows, so its parent event is ambiguous.",
        "check": "Resolve the duplicated Site_Code in Site Data first; the net rows "
                 "will then load on rerun.",
    },
    "MISSING_SITE_CODE": {
        "title": "Site_Code is blank",
        "meaning": "The row has no Site_Code, so no event can be found for it.",
        "check": "Use other columns (date, pool, crew, comments) to identify the site "
                 "and fill in the Site_Code in Access.",
    },
    "MISSING_SOURCE_ID": {
        "title": "Access ID is blank",
        "meaning": "The source row has no Access ID, so lineage cannot be recorded.",
        "check": "Check the Access table for a corrupted or pasted-in row.",
    },
    "MISSING_SPECIES": {
        "title": "Species is blank",
        "meaning": "A catch row without a species cannot be stored as a catch summary.",
        "check": "Fill in the species from the field sheet, or confirm the row is junk.",
    },
    "INVALID_CATCH_COUNT": {
        "title": "Catch is not a whole number",
        "meaning": "The Catch value could not be read as an integer (text, decimal, "
                   "or symbols).",
        "check": "Look at the raw value below and correct it in Access.",
    },
    "SITE_LOAD_ERROR": {
        "title": "Database error loading Site Data",
        "meaning": "An unexpected SQL error happened while inserting this row. "
                   "The message shows the error.",
        "check": "Usually a value too long or of the wrong type. Send the message to the ETL maintainer.",
    },
    "NET_LOAD_ERROR": {
        "title": "Database error loading Net Data",
        "meaning": "An unexpected SQL error happened while inserting this row.",
        "check": "Send the message to the ETL maintainer.",
    },
    "FISH_COUNT_LOAD_ERROR": {
        "title": "Database error loading Fish Count",
        "meaning": "An unexpected SQL error happened while inserting this row.",
        "check": "Send the message to the ETL maintainer.",
    },
    "SITE_DUPLICATE_GROUP_LOAD_ERROR": {
        "title": "Database error loading a duplicate site group",
        "meaning": "An unexpected SQL error happened while loading duplicate Site Data rows.",
        "check": "Send the message to the ETL maintainer.",
    },
    # ---------- quality issues (loaded) ----------
    "EXACT_DUPLICATE_SITE_RECORD": {
        "title": "Site Data entered twice (identical)",
        "meaning": "Two Site Data rows are identical except for their Access ID. They "
                   "were merged into one event and both IDs are kept in lineage.",
        "check": "Confirm it is a double entry. Usually 'Accepted as is'.",
    },
    "DUPLICATE_SITE_CODE_DIFFERING_PAYLOAD": {
        "title": "Same Site_Code, different site data",
        "meaning": "Several Site Data rows share a Site_Code but contain different "
                   "values. They were kept as separate events; child rows using only "
                   "the Site_Code could not be attached.",
        "check": "Decide whether these are truly different events (give them distinct "
                 "codes) or one event entered inconsistently (fix and merge).",
    },
    "DIFFERING_DUPLICATE_SITE_CODE": {
        "title": "Same Site_Code, different site data",
        "meaning": "Several Site Data rows share a Site_Code but differ in content.",
        "check": "Decide whether they are separate events or one event entered twice.",
    },
    "MISSING_NET_NUMBER_CODE": {
        "title": "Net number is blank",
        "meaning": "The Net Data row has no Net_Number_Code. It was loaded by its "
                   "Access ID, but Fish Count rows cannot link to it by net code.",
        "check": "Fill in the net number from the field sheet if known.",
    },
    "DUPLICATE_NET_BUSINESS_KEY_EXACT": {
        "title": "Net entered twice (identical)",
        "meaning": "Several Net Data rows share Site_Code + Net_Number_Code with "
                   "identical values.",
        "check": "Likely a double entry. Confirm, then delete the extra row in Access "
                 "or accept as is.",
    },
    "DUPLICATE_NET_BUSINESS_KEY_DIFFERING": {
        "title": "Same net code, different net data",
        "meaning": "Several Net Data rows share Site_Code + Net_Number_Code but contain "
                   "different values, so catches linked by net code are ambiguous.",
        "check": "Check whether the net numbers were mis-entered (e.g. two N2s that "
                 "should be N2 and N3).",
    },
    "SOAK_TIME_MISMATCH": {
        "title": "Soak time disagrees with set/pull times",
        "meaning": "The recorded Soak_Time differs from the hours between set and pull "
                   "timestamps. The recorded value was kept.",
        "check": "Check set/pull dates and times (AM/PM, wrong day) against the field sheet.",
    },
    "SOURCE_NET_KEY_MISSING": {
        "title": "Catch has no net number",
        "meaning": "The Fish Count row has no Net_Number_Code. Its event was unique, so "
                   "the catch was kept at event level (not tied to a specific net).",
        "check": "If the net is known, add Net_Number_Code in Access. Otherwise accept "
                 "event-level catch.",
    },
    "SOURCE_NET_EXISTS_NOT_LOADED": {
        "title": "Catch's net exists in Access but was not loaded",
        "meaning": "A matching Net Data row exists in Access but did not load (usually "
                   "because the net row itself was rejected). The catch was kept at "
                   "event level.",
        "check": "Fix the related Net Data reject first; the catch will link on rerun.",
    },
    "NET_PARENT_NOT_FOUND": {
        "title": "Catch's net not found",
        "meaning": "No Net Data row exists for the catch's Site_Code + Net_Number_Code. "
                   "The catch was kept at event level.",
        "check": "Look for a missing or mistyped net row in Access.",
    },
    "AMBIGUOUS_NET_PARENT": {
        "title": "Catch's net code matches several nets",
        "meaning": "The net code matches more than one loaded net, so the catch was kept "
                   "at event level rather than guessing.",
        "check": "Resolve the duplicate net rows for that site.",
    },
    "UNRECOGNIZED_REMOVED_VALUE": {
        "title": "'Removed' value not understood",
        "meaning": "The Removed column holds a value that is not clearly yes/no. The "
                   "original text was kept.",
        "check": "Confirm what the value means and standardise it in Access.",
    },
    "NEGATIVE_CATCH_COUNT": {
        "title": "Negative catch",
        "meaning": "Catch is below zero. The value was kept as-is for review.",
        "check": "Check the field sheet; probably a data-entry error.",
    },
}

STATUS_LABELS = {
    "OPEN": "Open",
    "IN_REVIEW": "In review",
    "NEEDS_SOURCE_FIX": "Needs source fix",
    "NEEDS_RULE_CHANGE": "Needs ETL rule change",
    "ACCEPTED_AS_IS": "Accepted as is",
    "RESOLVED": "Resolved",
}


# One entry per dataset shown in the portal.
DATASETS = [
    {
        "key": "catch",
        "view": "irbs.vw_harvest_catch",
        "title": "Catch by site (Fish Count + Site Data)",
        "replaces": "Wes Query, Modeling_Fish Count, Daily Fish Count, Query1",
        "description": "One row per Fish Count record, already joined to its site, date, "
                       "location, environment and (where known) net. This is the starting "
                       "table for catch and predictive models.",
        "notes": "catch_level = EVENT means the net could not be identified, so the catch "
                 "counts toward the site but has no net effort. Rejected rows (e.g. Site_Code "
                 "with no Site Data) are not included; see the QA report.",
        "order": "sample_date, site_code, species_code",
        "cols": {
            "catch_count": "Number of fish of this species in this Fish Count row.",
            "spawning_scars": "Spawning scars value from Fish Count.",
            "condition": "Condition value from Fish Count.",
            "catch_level": "NET = linked to a specific net; EVENT = linked to the site only.",
            "net_resolution_status": "How the net link was resolved (e.g. SOURCE_NET_KEY_MISSING).",
            "source_fish_count_id": "Access Fish Count.ID.",
        },
    },
    {
        "key": "catch_per_net",
        "view": "irbs.vw_harvest_catch_per_net",
        "title": "Catch per net (fish per yard / per net-hour)",
        "replaces": "fish/yd net",
        "description": "Catch summed by net and species, with effort: catch per yard, per "
                       "100 yards and per net-hour. Only catches linked to a specific net.",
        "notes": "Nets where a species was not caught have no row for that species (no zero rows).",
        "order": "sample_date, site_code, net_number_code, species_code",
        "cols": {
            "catch_per_yard": "catch_count / yardage.",
            "catch_per_100_yards": "catch_count × 100 / yardage.",
            "catch_per_net_hour": "catch_count / soak_time_hours.",
        },
    },
    {
        "key": "net_effort",
        "view": "irbs.vw_harvest_net_effort",
        "title": "Net effort (Net Data)",
        "replaces": "Total Net Yardage, Modeling_Net Yards",
        "description": "One row per net set with gear, yardage, set/pull times and soak time, "
                       "plus the total catch in that net.",
        "notes": "",
        "order": "sample_date, site_code, net_number_code",
        "cols": {
            "set_datetime": "When the net was set.",
            "pull_datetime": "When the net was pulled.",
            "net_total_catch": "Total fish counted in this net (all species).",
            "species_count": "Number of species recorded in this net.",
            "source_net_id": "Access Net Data.ID.",
        },
    },
    {
        "key": "site_event",
        "view": "irbs.vw_harvest_site_event",
        "title": "Sites / sampling events (Site Data)",
        "replaces": "Site Data Query",
        "description": "One row per sampling event with location, environment, crew, and "
                       "totals: nets, yardage, soak hours, catch and weight.",
        "notes": "",
        "order": "sample_date, site_code",
        "cols": {
            "fishermen": "Commercial fishers on the crew.",
            "data_collector": "Who recorded the data.",
            "tagging_activity": "1 = tagging happened at this event.",
            "net_count": "Number of nets set.",
            "total_yardage": "Total yards of net.",
            "total_soak_hours": "Sum of soak hours across nets.",
            "total_catch": "Total fish counted (all species).",
            "total_catch_removed": "Fish counted where Removed = yes.",
            "total_weight_kg": "Sum of species-group weights (kg) from Weight Data.",
            "source_site_id": "Access Site Data.ID.",
        },
    },
    {
        "key": "weights",
        "view": "irbs.vw_harvest_weights_by_site",
        "title": "Weights by site (Weight Data)",
        "replaces": "Weight_Site_Summaries",
        "description": "One row per Weight Data record, with kg for bighead, silver, grass, "
                       "hybrid and unsorted carp.",
        "notes": "total_kg is the sum of the groups; recorded_total_kg is the total typed into Access.",
        "order": "sample_date, site_code",
        "cols": {
            "bighead_kg": "Bighead carp (BHCP) kg.",
            "silver_kg": "Silver carp (SVCP) kg.",
            "grass_kg": "Grass carp (GSCP) kg.",
            "hybrid_kg": "Hybrid carp kg.",
            "unsorted_kg": "Unsorted species kg.",
            "total_kg": "Sum of the group weights.",
            "recorded_total_kg": "Total_Weight as recorded in Access.",
        },
    },
    {
        "key": "individual_fish",
        "view": "irbs.vw_harvest_individual_fish",
        "title": "Individual fish (bycatch, tagged, recaptured, SeaCarp)",
        "replaces": "Bycatch Data, Jaw Tags, Acoustic Tags, SeaCarp tables",
        "description": "One row per individual fish measured or tagged, with length, weight, "
                       "sex, gonad stage, ages and tag numbers.",
        "notes": "observation_role says which Access table the fish came from.",
        "order": "sample_date, site_code",
        "cols": {
            "observation_role": "BYCATCH, TAGGED, TAG_RECAPTURE, WORKED_UP (SeaCarp), LEADING_EDGE, OTHER.",
            "length_mm": "Total length (mm).",
            "fork_length_mm": "Fork length (mm).",
            "weight_g": "Weight (g).",
            "age_ps": "Age from pectoral spine.",
            "age_pc": "Age from post-cleithrum.",
            "age_combined": "Combined age estimate.",
            "jaw_tag_numbers": "Jaw tag number(s) on this fish.",
            "acoustic_tag_numbers": "Acoustic tag number(s) on this fish.",
            "included_in_catch_count": "1 = this fish is also counted in Fish Count.",
        },
    },
    {
        "key": "tag_deployments",
        "view": "irbs.vw_harvest_tag_deployments",
        "title": "Tags put on fish (jaw + acoustic)",
        "replaces": "Jaw Tagged Count, Query3",
        "description": "One row per tag deployment, with where/when the fish was tagged, the "
                       "fish's species and size, and how many times it was recaptured.",
        "notes": "",
        "order": "sample_date, tag_type, tag_number",
        "cols": {
            "tag_type": "JAW or ACOUSTIC.",
            "tag_number": "Tag number.",
            "release_or_kill": "What happened to the fish after tagging.",
            "recapture_count": "Number of recaptures recorded for this tag.",
            "last_recapture_date": "Most recent recapture date.",
        },
    },
    {
        "key": "tag_recaptures",
        "view": "irbs.vw_harvest_tag_recaptures",
        "title": "Tag recaptures",
        "replaces": "Recapture Count",
        "description": "One row per recapture, with the recapture site/date and the original "
                       "tagging site/date, species and days at large.",
        "notes": "tag_resolution_status = DEPLOYMENT_NOT_FOUND means the original tagging record "
                 "could not be matched.",
        "order": "recapture_date, tag_number",
        "cols": {
            "days_at_large": "Days between tagging and recapture.",
            "tag_removed": "1 = tag/fish removed at recapture.",
        },
    },
    {
        "key": "translocation",
        "view": "irbs.vw_harvest_translocation",
        "title": "Translocation / Tagging Info",
        "replaces": "Tagging Info",
        "description": "Counts and weights of fish released by agency and release pool.",
        "notes": "",
        "order": "sample_date, site_code",
        "cols": {
            "agency": "Agency.",
            "translocated": "1 = fish were translocated.",
            "release_pool": "Pool where fish were released.",
            "fish_count": "Number of fish.",
            "weight_kg": "Weight (kg).",
        },
    },
]


# ---------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------
def get_password(user: str) -> str:
    env = os.environ.get("IRBS_SQL_PWD")
    if env:
        return env
    if sys.stdin.isatty() and "PYCHARM_HOSTED" not in os.environ:
        return getpass.getpass(f"Password for {user}: ")
    print("(This console cannot hide typing - the password will be visible.)")
    return input(f"Password for {user}: ")


def _odbc_value(v: str) -> str:
    """Quote a value for an ODBC connection string so ; = { } cannot inject attributes."""
    return "{" + str(v).replace("}", "}}") + "}"


def connect(server: str, database: str, user: str | None, password: str | None = None,
            quiet: bool = False, query_timeout: int | None = None, app_name: str = "IRBS Data Hub"):
    import pyodbc

    drivers = pyodbc.drivers()
    wanted = os.environ.get("IRBS_SQL_DRIVER")
    order = ([wanted] if wanted else []) + ["ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server",
                                            "SQL Server Native Client 11.0", "SQL Server"]
    driver = next((d for d in order if d in drivers), None)
    if not driver:
        raise RuntimeError(f"No SQL Server ODBC driver found on this machine. Installed: {drivers}")
    trust = os.environ.get("IRBS_SQL_TRUST_SERVER_CERTIFICATE", "yes").strip().lower()
    parts = [f"DRIVER={{{driver}}}", f"SERVER={_odbc_value(server)}", f"DATABASE={_odbc_value(database)}",
             f"TrustServerCertificate={'yes' if trust in ('1', 'yes', 'true') else 'no'}",
             f"APP={_odbc_value(app_name)}"]
    if "18" in driver or "17" in driver:
        parts.append("Encrypt=yes")
    if user:
        pwd = password if password is not None else get_password(user)
        parts += [f"UID={_odbc_value(user)}", f"PWD={_odbc_value(pwd)}"]
    else:
        parts.append("Trusted_Connection=yes")
    if not quiet:
        print(f"Connecting to {server} / {database} ({driver}) ...")
    conn = pyodbc.connect(";".join(parts), timeout=int(os.environ.get("IRBS_LOGIN_TIMEOUT", "20")))
    if query_timeout:
        conn.timeout = int(query_timeout)  # seconds; a query running longer is cancelled
    return conn


def read_view(conn, ds: dict) -> pd.DataFrame:
    cur = conn.cursor()
    cur.execute(f"SELECT * FROM {ds['view']} ORDER BY {ds['order']}")
    cols = [c[0] for c in cur.description]
    return pd.DataFrame.from_records(cur.fetchall(), columns=cols)


# ---------------------------------------------------------------------
# Packing: columnar JSON, repeated strings dictionary-encoded
# ---------------------------------------------------------------------
def _val(v):
    if v is None:
        return None
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, Decimal):
        f = float(v)
        return int(f) if f.is_integer() else f
    if isinstance(v, datetime) or isinstance(v, pd.Timestamp):
        if pd.isna(v):
            return None
        return v.strftime("%Y-%m-%d %H:%M") if (v.hour or v.minute) else v.strftime("%Y-%m-%d")
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, time):
        return v.strftime("%H:%M")
    if hasattr(v, "item"):
        v = v.item()
    if isinstance(v, float):
        if v != v:
            return None
        return int(v) if v.is_integer() and abs(v) < 1e15 else round(v, 6)
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    return v


def col_kind(name: str, values: list) -> str:
    nonnull = [v for v in values if v is not None]
    if not nonnull:
        return "text"
    if all(isinstance(v, (int, float)) for v in nonnull):
        return "number"
    if all(isinstance(v, str) and len(v) in (10, 16) and v[4] == "-" and v[7] == "-" for v in nonnull[:200]):
        return "date"
    return "text"


def pack_dataset(ds: dict, df: pd.DataFrame) -> dict:
    cols_out = []
    for name in df.columns:
        vals = [_val(v) for v in df[name].tolist()]
        kind = col_kind(name, vals)
        entry = {"name": name, "kind": kind,
                 "desc": ds["cols"].get(name) or COMMON_COLS.get(name, "")}
        if kind == "text":
            uniq = {}
            for v in vals:
                if v is not None and v not in uniq:
                    uniq[v] = len(uniq)
            if len(uniq) <= max(50, len(vals) // 3):
                entry["dict"] = list(uniq)
                entry["data"] = [None if v is None else uniq[v] for v in vals]
            else:
                entry["data"] = vals
        else:
            entry["data"] = vals
        cols_out.append(entry)
    return {k: ds[k] for k in ("key", "view", "title", "replaces", "description", "notes")} | {
        "rows": len(df), "columns": cols_out}


