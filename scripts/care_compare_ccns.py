"""List the CCNs that have a Medicare Care Compare profile, by profile type.

Reads the CMS Provider Data Catalog (the data behind medicare.gov/care-compare): Hospital General
Information (hospital profiles), Inpatient Rehabilitation Facility and Long-Term Care Hospital
General Information. Writes data/cms/care_compare_ccns.csv (ccn, profile). The site uses it to
link each CCN to the right Care Compare page, and only when that page exists.
"""

from __future__ import annotations

import csv
import io
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "cms" / "care_compare_ccns.csv"
META = "https://data.cms.gov/provider-data/api/1/metastore/schemas/dataset/items"
UA = {"User-Agent": "hpt-hub/0.1 (open hospital price transparency research registry; "
                    "+https://github.com/Moqri-Lab/hospitals)"}
WANT = {  # profile -> dataset title
    "hospital": "Hospital General Information",
    "inpatient-rehabilitation": "Inpatient Rehabilitation Facility - General Information",
    "long-term-care-hospital": "Long-Term Care Hospital - General Information",
}


def main() -> None:
    s = requests.Session()
    s.headers.update(UA)
    items = s.get(META, timeout=120).json()
    by_title = {i.get("title", "").strip(): i for i in items}
    rows = []
    for profile, title in WANT.items():
        ds = by_title.get(title)
        if not ds:
            near = [t for t in by_title if title.split(" - ")[0].lower() in t.lower()]
            sys.exit(f"dataset not found: {title!r}; similar: {near}")
        url = next(d["downloadURL"] for d in ds["distribution"] if d.get("downloadURL", "").endswith(".csv"))
        r = s.get(url, timeout=300)
        r.raise_for_status()
        rd = csv.DictReader(io.StringIO(r.content.decode("utf-8-sig", "replace")))
        col = next(c for c in rd.fieldnames if c.strip().lower() in ("facility id", "cms certification number (ccn)", "ccn"))
        ccns = sorted({x[col].strip().zfill(6) for x in rd if x[col].strip()})
        print(f"{profile}: {len(ccns)} CCNs from {title} ({ds['identifier']}, column {col!r})")
        rows += [(c, profile) for c in ccns]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ccn", "profile"])
        w.writerows(rows)


if __name__ == "__main__":
    main()
