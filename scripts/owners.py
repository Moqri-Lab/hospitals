"""Owners of each hospital (CMS Hospital All Owners) and the public company behind it (SEC EDGAR).

1. CMS Hospital Enrollments (CCN -> enrollment) and Hospital All Owners (enrollment -> owners), both
   public on data.cms.gov. Only organization owners are kept (no individuals).
2. SEC EDGAR company list: an organization owner flagged as a parent company is matched by name
   to an EDGAR registrant, and the registrant's EIN is read from its EDGAR record.

Writes data/registry/hospital_owners.csv (ccn, owner, role, pct, parent, for_profit, cik, sec_name,
sec_ein). The site links non-tax-exempt EINs to the parent company's SEC filings.

Usage: python scripts/owners.py
"""

from __future__ import annotations

import csv
import io
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


def norm(s: str) -> str:
    w = re.sub(r"[^A-Z0-9 ]", " ", s.upper().replace("&", " AND ")).split()
    return " ".join(x for x in w if x not in STOP)


def latest_csv(sess, cat, title: str) -> str:
    for ds in cat["dataset"]:
        if ds.get("title", "").strip().lower() == title.lower():
            d = [x for x in ds.get("distribution", []) if str(x.get("downloadURL", "")).endswith(".csv")]
            d.sort(key=lambda x: x.get("temporal", "") or x.get("modified", ""), reverse=True)
            print(f"{title}: {d[0]['downloadURL']}")
            return d[0]["downloadURL"]
    raise SystemExit(f"not in catalog: {title}")


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
                        "parent": col(r, "PARENT COMPANY - OWNER"), "for_profit": col(r, "FOR PROFIT - OWNER")})
    print(f"{len(out)} organization owner rows for {len({o['ccn'] for o in out})} CCNs")

    # SEC EDGAR: match owner names to registrants, read each match's EIN.
    sec = {}
    try:
        t = s.get("https://www.sec.gov/files/company_tickers.json", timeout=120)
        print(f"SEC company list: HTTP {t.status_code}")
        if t.ok:
            for v in t.json().values():
                sec.setdefault(norm(v["title"]), (str(v["cik_str"]).zfill(10), v["title"]))
    except requests.RequestException as e:
        print(f"SEC company list failed: {e}")
    eins: dict[str, str] = {}
    for o in out:
        m = sec.get(norm(o["owner"])) if o["owner"] else None
        o.update(cik="", sec_name="", sec_ein="")
        if not m:
            continue
        cik, name = m
        if cik not in eins:
            time.sleep(0.2)  # SEC asks for at most 10 requests a second
            j = s.get(f"https://data.sec.gov/submissions/CIK{cik}.json", timeout=60)
            eins[cik] = (j.json().get("ein") or "") if j.ok else ""
        o.update(cik=cik, sec_name=name, sec_ein=eins[cik])
    print(f"{len({o['ccn'] for o in out if o['cik']})} CCNs with an SEC-registered owner; "
          f"registrants: {sorted({o['sec_name'] for o in out if o['cik']})}")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    cols = ["ccn", "owner", "role", "pct", "parent", "for_profit", "cik", "sec_name", "sec_ein"]
    with OUT.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for o in sorted(out, key=lambda o: (o["ccn"], o["owner"])):
            w.writerow(o)


if __name__ == "__main__":
    main()
