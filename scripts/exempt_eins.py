"""Which hospital EINs belong to tax-exempt organizations (and so have a ProPublica Nonprofit Explorer page).

Reads the IRS Exempt Organizations Business Master File extract (public, from
https://www.irs.gov/pub/irs-soi/), keeps the EINs that appear in data/ccn_ein.csv, and writes
data/registry/exempt_eins.csv (ein, irs_name, subsection). The site links those EINs to
https://projects.propublica.org/nonprofits/organizations/<EIN digits>.

Usage: python scripts/exempt_eins.py
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
XW = ROOT / "data" / "ccn_ein.csv"
OUT = ROOT / "data" / "registry" / "exempt_eins.csv"
BASE = "https://www.irs.gov/pub/irs-soi"
FILES = ["eo1.csv", "eo2.csv", "eo3.csv", "eo4.csv", "eo_pr.csv", "eo_xx.csv"]
UA = {"User-Agent": "hpt-hub/0.1 (open hospital price transparency research registry; "
                    "+https://github.com/Moqri-Lab/hospitals)"}


def main() -> None:
    want = {r["ein"].replace("-", "") for r in csv.DictReader(XW.open()) if r["ein"]}
    s = requests.Session()
    s.headers.update(UA)
    found: dict[str, tuple[str, str]] = {}
    for f in FILES:
        r = s.get(f"{BASE}/{f}", timeout=600)
        if r.status_code != 200:
            print(f"{f}: HTTP {r.status_code}")
            continue
        n = 0
        for row in csv.DictReader(io.StringIO(r.content.decode("latin-1"))):
            n += 1
            e = row.get("EIN", "").strip()
            if e in want:
                found[e] = (row.get("NAME", "").strip(), row.get("SUBSECTION", "").strip())
        print(f"{f}: {n} organizations, {len(found)} of {len(want)} hospital EINs found so far", flush=True)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["ein", "irs_name", "subsection"])
        for e in sorted(found):
            w.writerow([f"{e[:2]}-{e[2:]}", *found[e]])


if __name__ == "__main__":
    main()
