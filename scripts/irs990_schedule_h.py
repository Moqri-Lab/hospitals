"""Hospital facilities listed on IRS Form 990 Schedule H, Part V Section A, with the filer's EIN.

Nonprofit hospital organizations list each licensed hospital facility they operate (name and
address) on Schedule H. This script reads the IRS e-file XML releases (public, from
https://apps.irs.gov/pub/epostcard/990/xml/), keeps the latest return per EIN, and writes
data/registry/irs990_hospital_facilities.csv. The hub matches those facilities to CCNs by state,
ZIP and name or street address, to fill EINs that price-file names don't give.

Usage: python scripts/irs990_schedule_h.py 2025 2024
"""

from __future__ import annotations

import csv
import io
import re
import sys
import tempfile
import zipfile
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "registry" / "irs990_hospital_facilities.csv"
BASE = "https://apps.irs.gov/pub/epostcard/990/xml"
UA = {"User-Agent": "hpt-hub/0.1 (open hospital price transparency research registry; "
                    "+https://github.com/Moqri-Lab/hospitals)"}


def tag(xml: str, name: str) -> str:
    m = re.search(rf"<{name}>([^<]*)</{name}>", xml)
    return m.group(1).strip() if m else ""


def facilities(xml: str) -> list[dict]:
    out = []
    for g in re.findall(r"<HospitalFacilitiesGrp>(.*?)</HospitalFacilitiesGrp>", xml, re.S):
        out.append({
            "facility_name": tag(g, "BusinessNameLine1Txt") or tag(g, "BusinessNameLine1"),
            "facility_name2": tag(g, "BusinessNameLine2Txt"),
            "address": tag(g, "AddressLine1Txt"),
            "city": tag(g, "CityNm"),
            "state": tag(g, "StateAbbreviationCd"),
            "zip": tag(g, "ZIPCd")[:5],
        })
    return out


def main(years: list[str]) -> None:
    sess = requests.Session()
    sess.headers.update(UA)
    rows: dict[str, tuple[str, list[dict]]] = {}  # ein -> (tax period, facility rows)
    for year in years:
        idx = sess.get(f"{BASE}/{year}/index_{year}.csv", timeout=300)
        idx.raise_for_status()
        want: dict[str, set[str]] = {}
        for r in csv.DictReader(io.StringIO(idx.text)):
            if r.get("RETURN_TYPE", "").strip() == "990" and r.get("XML_BATCH_ID"):
                want.setdefault(r["XML_BATCH_ID"].strip(), set()).add(r["OBJECT_ID"].strip())
        print(f"{year}: {sum(map(len, want.values()))} Form 990 returns in {len(want)} batches", flush=True)
        for batch, ids in sorted(want.items()):
            url = f"{BASE}/{year}/{batch}.zip"
            with tempfile.NamedTemporaryFile(suffix=".zip") as tmp:
                with sess.get(url, stream=True, timeout=600) as resp:
                    if resp.status_code != 200:
                        print(f"  {batch}: HTTP {resp.status_code}", flush=True)
                        continue
                    for chunk in resp.iter_content(1 << 20):
                        tmp.write(chunk)
                tmp.flush()
                n = 0
                with zipfile.ZipFile(tmp.name) as z:
                    for name in z.namelist():
                        oid = Path(name).name.split("_")[0]
                        if oid not in ids:
                            continue
                        data = z.read(name)
                        if b"HospitalFacilitiesGrp" not in data:
                            continue
                        xml = data.decode("utf-8", "replace")
                        ein = tag(xml, "EIN")
                        period = tag(xml, "TaxPeriodEndDt")
                        fac = facilities(xml)
                        if not ein or not fac:
                            continue
                        filer = tag(xml.split("</Filer>")[0], "BusinessNameLine1Txt")
                        for f in fac:
                            f.update(ein=f"{ein[:2]}-{ein[2:]}", filer_name=filer, tax_period_end=period)
                        if ein not in rows or period > rows[ein][0]:
                            rows[ein] = (period, fac)
                        n += 1
                print(f"  {batch}: {n} returns with Schedule H facilities ({len(rows)} EINs so far)", flush=True)
    cols = ["ein", "filer_name", "tax_period_end", "facility_name", "facility_name2", "address", "city", "state", "zip"]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for ein in sorted(rows):
            for r in rows[ein][1]:
                w.writerow({k: r.get(k, "") for k in cols})
    print(f"{sum(len(v[1]) for v in rows.values())} facilities from {len(rows)} EINs -> {OUT}")


if __name__ == "__main__":
    main(sys.argv[1:] or ["2025", "2024"])
