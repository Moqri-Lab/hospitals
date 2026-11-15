"""Latest Federal single audit per hospital EIN, from the Federal Audit Clearinghouse (fac.gov).

Government hospitals (and nonprofits spending $750,000+ of federal awards a year) file a single
audit with financial statements. The IRS does not require a Form 990 from government entities, so
for them the audit is the public financial record. Queries the public FAC API
(api.fac.gov, api.data.gov DEMO_KEY, EINs batched) and writes data/registry/fac_audits.csv
(ein, report_id, audit_year, auditee_name). The site links an EIN to
https://app.fac.gov/dissemination/summary/<report_id>.

Usage: python scripts/fac_audits.py
"""

from __future__ import annotations

import csv
import os
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "registry" / "fac_audits.csv"
API = "https://api.fac.gov/general"
UA = {"User-Agent": "hpt-hub/0.1 (open hospital price transparency research registry; "
                    "+https://github.com/Moqri-Lab/hospitals)",
      "X-Api-Key": os.environ.get("FAC_API_KEY") or "DEMO_KEY"}


def main() -> None:
    eins = sorted({r["ein"].replace("-", "") for r in csv.DictReader((ROOT / "data" / "ccn_ein.csv").open())
                   if r["ein"]})
    s = requests.Session()
    s.headers.update(UA)
    best: dict[str, dict] = {}
    for i in range(0, len(eins), 60):
        chunk = eins[i:i + 60]
        for attempt in range(3):
            r = s.get(API, params={"auditee_ein": f"in.({','.join(chunk)})",
                                   "select": "report_id,auditee_ein,auditee_name,audit_year,fac_accepted_date",
                                   "order": "audit_year.desc", "limit": "5000"}, timeout=120)
            if r.status_code != 429:
                break
            time.sleep(60)
        if not r.ok:
            print(f"batch {i}: HTTP {r.status_code} {r.text[:200]}")
            continue
        for a in r.json():
            e = str(a["auditee_ein"])
            if e not in best or str(a["audit_year"]) > str(best[e]["audit_year"]):
                best[e] = a
        print(f"batch {i}: {len(best)} EINs with an audit so far (remaining quota "
              f"{r.headers.get('X-RateLimit-Remaining')})", flush=True)
        time.sleep(2)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ein", "report_id", "audit_year", "auditee_name"])
        for e in sorted(best):
            a = best[e]
            w.writerow([f"{e[:2]}-{e[2:]}", a["report_id"], a["audit_year"], a["auditee_name"]])
    print(f"{len(best)} of {len(eins)} hospital EINs have a FAC single audit")


if __name__ == "__main__":
    main()
