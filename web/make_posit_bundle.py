"""Copies the shared files into web/ so the folder can be deployed on its own:
    python make_posit_bundle.py
    rsconnect deploy api --entrypoint app:app --title "IRBS Data Hub" .
"""
import shutil
from pathlib import Path

here = Path(__file__).resolve().parent
for name in ("portal_core.py", "hub.html", "xlsx.mini.min.js"):
    shutil.copy2(here.parent / name, here / name)
    print("copied", name)
