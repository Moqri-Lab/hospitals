"""Which hospital EINs belong to tax-exempt organizations (and so have a ProPublica Nonprofit Explorer page).

Reads the IRS Exempt Organizations Business Master File extract (public, from
https://www.irs.gov/pub/irs-soi/), keeps the EINs that appear in data/ccn_ein.csv, and writes
data/registry/exempt_eins.csv (ein, irs_name, subsection, filing_req_cd, asset_amt, income_amt,
revenue_amt). The amounts and filing code show which EINs file a full Form 990 with financials and
which only file a 990-N postcard or are covered by a group return. The site links those EINs to
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
    fac = ROOT / "data" / "registry" / "irs990_hospital_facilities.csv"
    if fac.exists():  # Schedule H filers too: often the system EIN that carries the finances
        want |= {r["ein"].replace("-", "") for r in csv.DictReader(fac.open()) if r["ein"]}
    s = requests.Session()
    s.headers.update(UA)
    found: dict[str, tuple] = {}
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
                found[e] = tuple(row.get(k, "").strip() for k in
                                 ("NAME", "SUBSECTION", "FILING_REQ_CD", "ASSET_AMT", "INCOME_AMT", "REVENUE_AMT"))
        print(f"{f}: {n} organizations, {len(found)} of {len(want)} hospital EINs found so far", flush=True)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["ein", "irs_name", "subsection", "filing_req_cd", "asset_amt", "income_amt", "revenue_amt"])
        for e in sorted(found):
            w.writerow([f"{e[:2]}-{e[2:]}", *found[e]])


if __name__ == "__main__":
    main()
