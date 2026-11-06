"""
IRBS Harvest Data Portal
========================
Builds ONE self-contained HTML file (the "portal") from the irbs.vw_harvest_*
views. Anyone can open it in a browser to:
  * browse each dataset (catch, net effort, weights, tags, ...)
  * filter by year, reach, species, site code or any text
  * see what every column means
  * download the filtered rows, the full dataset, or everything as Excel

Nothing to install for the people using it. Re-run this script to refresh.

HOW TO RUN (ETL project .venv active, or any Python with pandas + pyodbc)
-------------------------------------------------------------------------
    python data_portal/46_build_data_portal.py

You are prompted for server, database, username and password
(leave username blank to use your Windows login).

Options
    --server / --database / --user     skip the prompts
    --out PATH                         output file
                                       (default data_portal/output/IRBS_Harvest_Data_Portal_<date>.html)
    --no-open                          don't open the browser
    --only catch,net_effort            build only some datasets
Password: prompted, or set environment variable IRBS_SQL_PWD.

Files that must sit next to this script:
    portal_template.html, xlsx.mini.min.js  (SheetJS, Apache-2.0)
Reads the views from data_portal/sql/45_create_harvest_analysis_views.sql.
"""

from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from datetime import datetime
from pathlib import Path

from portal_core import DATASETS, DEFAULT_DATABASE, DEFAULT_SERVER, HERE, connect, pack_dataset, read_view

def build(payload: dict, out: Path) -> None:
    tpl = (HERE / "portal_template.html").read_text(encoding="utf-8")
    lib = (HERE / "xlsx.mini.min.js").read_text(encoding="utf-8")
    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str).replace("</", "<\\/")
    html = tpl.replace("/*__XLSX_LIB__*/", lib.replace("</script", "<\\/script")).replace("/*__DATA__*/null", blob)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server")
    ap.add_argument("--database")
    ap.add_argument("--user")
    ap.add_argument("--out")
    ap.add_argument("--no-open", action="store_true")
    ap.add_argument("--only", help="comma-separated dataset keys")
    a = ap.parse_args()

    for f in ("portal_template.html", "xlsx.mini.min.js"):
        if not (HERE / f).exists():
            raise SystemExit(f"{f} must be in the same folder as this script: {HERE}")
    try:
        import pyodbc  # noqa: F401
    except ImportError:
        raise SystemExit("pyodbc is not installed. Run:  pip install pyodbc pandas")

    server = a.server or input(f"Server [{DEFAULT_SERVER}]: ").strip() or DEFAULT_SERVER
    database = a.database or input(f"Database [{DEFAULT_DATABASE}]: ").strip() or DEFAULT_DATABASE
    user = a.user if a.user is not None else input("Username (blank = Windows login): ").strip()

    wanted = set(a.only.split(",")) if a.only else None
    conn = connect(server, database, user or None)
    datasets = []
    for ds in DATASETS:
        if wanted and ds["key"] not in wanted:
            continue
        try:
            df = read_view(conn, ds)
        except Exception as exc:  # view missing / no permission: skip, keep going
            print(f"  ! {ds['view']}: skipped ({str(exc).splitlines()[0][:160]})")
            continue
        print(f"  {ds['view']}: {len(df):,} rows")
        datasets.append(pack_dataset(ds, df))
    conn.close()
    if not datasets:
        raise SystemExit("No datasets could be read. Have the views in 45_create_harvest_analysis_views.sql been created?")

    payload = {"meta": {"generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                        "source": f"{server} / {database}"},
               "datasets": datasets}
    out = Path(a.out) if a.out else HERE / "output" / f"IRBS_Harvest_Data_Portal_{datetime.now():%Y%m%d}.html"
    build(payload, out)
    print(f"\nPortal written: {out.resolve()}  ({out.stat().st_size / 1e6:.1f} MB)")
    if not a.no_open:
        webbrowser.open(out.resolve().as_uri())


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(1)
