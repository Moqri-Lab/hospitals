"""Owners of each hospital (CMS Hospital All Owners) and the public company behind it (SEC EDGAR).

1. CMS Hospital Enrollments (CCN -> enrollment) and Hospital All Owners (enrollment -> owners), both
   public on data.cms.gov. Only organization owners are kept (no individuals).
2. Publicly traded hospital companies (PUBLIC below) matched by owner name, for a link to their
   SEC EDGAR filings.

Also writes data/registry/ccn_npi.csv (ccn, npi, enrolled_name) from the enrollments file.

Writes data/registry/hospital_owners.csv (ccn, owner, role, pct, for_profit, sec_name). The site links non-tax-exempt EINs to the parent company's SEC filings.

Usage: python scripts/owners.py
"""

from __future__ import annotations

import csv
import io
import json
import re
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "registry" / "hospital_owners.csv"
CATALOG = "https://data.cms.gov/data.json"
UA = {"User-Agent": "hpt-hub/0.1 (open hospital price transparency research registry; "
                    "+https://github.com/Moqri-Lab/hospitals)"}
STOP = {"INC", "INCORPORATED", "CORP", "CORPORATION", "CO", "COMPANY", "LLC", "LP", "LTD", "THE",
        "HOLDINGS", "HOLDING", "GROUP", "PLC", "NV", "DE", "OF", "AND"}


PUBLIC = [  # (EDGAR registrant name, pattern on the CMS owner name)
    ("HCA Healthcare, Inc.", r"^HCA HEALTHCARE\b|^HCA,? INC\b"),
    ("Tenet Healthcare Corp", r"^TENET HEALTHCARE CORP"),
    ("Community Health Systems Inc", r"^COMMUNITY HEALTH SYSTEMS,? INC|^CHS/COMMUNITY HEALTH SYSTEMS"),
    ("Universal Health Services Inc", r"^UNIVERSAL HEALTH SERVICES,? INC"),
    ("Encompass Health Corp", r"^ENCOMPASS HEALTH CORP"),
    ("Select Medical Holdings Corp", r"^SELECT MEDICAL (HOLDINGS )?CORP"),
    ("Acadia Healthcare Company, Inc.", r"^ACADIA HEALTHCARE COMPANY"),
    ("Ardent Health, Inc.", r"^ARDENT HEALTH(,? INC| PARTNERS)"),
    ("Surgery Partners, Inc.", r"^SURGERY PARTNERS,? INC"),
    ("Nutex Health Inc.", r"^NUTEX HEALTH"),
]


def norm(s: str) -> str:
    w = re.sub(r"[^A-Z0-9 ]", " ", s.upper().replace("&", " AND ")).split()
    return " ".join(x for x in w if x not in STOP)


def latest_csv(sess, cat, title: str) -> str:
    want = set(title.lower().split())
    # CMS publishes each monthly release as its own catalog entry, "<title> : YYYY-MM-DD": newest first.
    for ds in sorted(cat["dataset"], key=lambda d: re.findall(r"\d{4}-\d\d-\d\d", d.get("title", "")) or [""],
                     reverse=True):
        if want <= set(re.sub(r"[^a-z ]", " ", ds.get("title", "").lower()).split()):
            d = [x for x in ds.get("distribution", []) if str(x.get("downloadURL", "")).endswith(".csv")]
            def when(x):  # date in the file name first (uploads can be re-dated), then temporal, modified
                for v in (str(x.get("downloadURL", "")).rsplit("/", 1)[-1], x.get("temporal", ""),
                          x.get("modified", ""), json.dumps(x)):
                    ds = re.findall(r"(20\d\d)[-._/]?(\d\d)(?:[-._/]?(\d\d))?", str(v))
                    if ds:
                        return max((a, b, c or "00") for a, b, c in ds)
                return ("",)
            d.sort(key=when, reverse=True)
            for x in d[:3]:
                print(f"  candidate {when(x)}: {x.get('downloadURL')}")
            print(f"{ds.get('title')}: {d[0]['downloadURL']}")
            return d[0]["downloadURL"]
    near = [d.get("title") for d in cat["dataset"] if "hospital" in d.get("title", "").lower()]
    raise SystemExit(f"not in catalog: {title}; hospital titles: {near}")


def rows(sess, url: str) -> list[dict]:
    r = sess.get(url, timeout=600)
    r.raise_for_status()
    rd = list(csv.DictReader(io.StringIO(r.content.decode("utf-8-sig", "replace"))))
    print(f"  {len(rd)} rows; columns: {list(rd[0])[:40]}")
    return rd


def col(row: dict, *names: str) -> str:
    for n in names:
        for k in row:
            if k.strip().upper() == n:
                return (row[k] or "").strip()
    return ""


def main() -> None:
    s = requests.Session()
    s.headers.update(UA)
    cat = s.get(CATALOG, timeout=300).json()
    enr = rows(s, latest_csv(s, cat, "Hospital Enrollments"))
    own = rows(s, latest_csv(s, cat, "Hospital All Owners"))
    npis = {}  # (ccn, npi) -> enrolled organization name
    for r in enr:
        ccn, npi = col(r, "CCN", "PROVIDER CCN"), col(r, "NPI")
        if ccn and re.fullmatch(r"[12]\d{9}", npi):
            npis[(ccn.zfill(6), npi)] = col(r, "ORGANIZATION NAME")
    npi_out = ROOT / "data" / "registry" / "ccn_npi.csv"
    with npi_out.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ccn", "npi", "enrolled_name"])
        for (c, n), name in sorted(npis.items()):
            w.writerow([c, n, name])
    print(f"{len(npis)} CCN-NPI pairs for {len({c for c, _ in npis})} CCNs -> {npi_out}")
    ccn_of: dict[str, set[str]] = {}
    for r in enr:
        ccn = col(r, "CCN", "PROVIDER CCN")
        eid = col(r, "ENROLLMENT ID")
        if ccn and eid:
            ccn_of.setdefault(eid, set()).add(ccn.zfill(6))
    out = []
    for r in own:
        if col(r, "TYPE - OWNER") != "O":
            continue  # organizations only, no individuals
        for ccn in ccn_of.get(col(r, "ENROLLMENT ID"), ()):
            out.append({"ccn": ccn, "owner": col(r, "ORGANIZATION NAME - OWNER"),
                        "role": col(r, "ROLE TEXT - OWNER"), "pct": col(r, "PERCENTAGE OWNERSHIP"),
                        "for_profit": col(r, "FOR PROFIT - OWNER")})
    print(f"{len(out)} organization owner rows for {len({o['ccn'] for o in out})} CCNs")

    # Public companies that own US hospitals, matched by owner name. Each links to its SEC EDGAR
    # filings (the site builds the EDGAR company link from the name). SEC's API is not called.
    for o in out:
        o["sec_name"] = next((name for name, pat in PUBLIC if re.search(pat, o["owner"].upper())), "")
    print(f"{len({o['ccn'] for o in out if o['sec_name']})} CCNs with a publicly traded owner: "
          f"{sorted({o['sec_name'] for o in out if o['sec_name']})}")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    cols = ["ccn", "owner", "role", "pct", "for_profit", "sec_name"]
    with OUT.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for o in sorted(out, key=lambda o: (o["ccn"], o["owner"])):
            w.writerow(o)


if __name__ == "__main__":
    main()
