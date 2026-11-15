"""Confirm websites that only one source gives, by checking that the home page shows the hospital.

The compliance check blames a missing cms-hpt.txt on a hospital only when we are sure the website is
theirs (two sources agree, or its cms-hpt.txt names the hospital). For hospitals whose website comes
from one source only, this reads the website's home page (only the home page, no price data) and
keeps it as confirmed when the page shows the hospital's street address and ZIP, its city and ZIP,
or every distinctive word of its name. Writes data/registry/verified_websites.csv (ccn, domain,
evidence), which the site treats as a second source.

Usage: python scripts/verify_websites.py
"""

from __future__ import annotations

import csv
import os
import sys
from concurrent.futures import ThreadPoolExecutor, TimeoutError, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pandas as pd  # noqa: E402

from find_websites import address_shown, name_shown, page_text  # noqa: E402
from hpthub import http  # noqa: E402
from hpthub.registry import REG_DIR, ROOT  # noqa: E402

OUT = REG_DIR / "verified_websites.csv"


def check(sess, h) -> dict | None:
    d = h["hpt_txt_domain"]
    text = page_text(sess, d)
    if not text:
        return None
    ev = address_shown(text, h)
    if not ev:
        for n in dict.fromkeys([h["name"], h.get("pos_name", "")]):
            if n and name_shown(text, n):
                ev = f"home page shows the name '{n}'"
                break
    return {"ccn": h["ccn"], "domain": d, "evidence": ev} if ev else None


def main() -> None:
    cat = pd.read_csv(ROOT / "data" / "catalog.csv", dtype=str).fillna("").drop_duplicates("ccn")
    todo = cat[cat["txt_check_reason"].str.contains("not sure this is the hospital", regex=False)]
    print(f"{len(todo)} hospitals whose website only one source gives")
    sess = http.session(retries=1)
    ex = ThreadPoolExecutor(max_workers=32)
    futs = [ex.submit(check, sess, h) for _, h in todo.iterrows()]
    found = []
    try:
        for fut in as_completed(futs, timeout=20 * 60):
            try:
                r = fut.result()
            except Exception:  # noqa: BLE001
                r = None
            if r:
                found.append(r)
    except TimeoutError:
        print("time limit reached")
    old = {}
    if OUT.exists():
        with OUT.open(newline="") as f:
            old = {(r["ccn"], r["domain"]): r for r in csv.DictReader(f)}
    for r in found:
        old[(r["ccn"], r["domain"])] = r
    with OUT.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["ccn", "domain", "evidence"])
        w.writeheader()
        for k in sorted(old):
            w.writerow(old[k])
    print(f"{len(found)} of {len(todo)} confirmed -> {OUT}", flush=True)
    os._exit(0)


if __name__ == "__main__":
    main()
