"""Find websites for hospitals that no source gives one for, and keep only verified ones.

Two ways to propose a website for a hospital with no known domain:

  1. cms-hpt.txt cross-match: another website's cms-hpt.txt (already crawled, in
     data/discovery/hpt_txt_entries.csv) lists a location whose name matches the
     hospital (common for systems that file for hospitals under a shared website).
  2. Name guess: domains built from the hospital's name, e.g. "Wiregrass Medical
     Center" -> wiregrassmedicalcenter.org / .com, wiregrassmc.org, ...

A proposed website is kept only if its own home page shows the hospital's
address (ZIP code plus street number, or ZIP code plus city), so a hospital of
the same name in another state is not picked up. Guesses must also have a
cms-hpt.txt naming the hospital, or a home page naming it. Every kept website is
still checked by the normal crawl, which reads its cms-hpt.txt.

Only home pages and cms-hpt.txt files are fetched; price files are never touched.

Writes leads with source "inferred" to data/registry/domain_leads.csv and the
evidence for each to data/registry/inferred_websites.csv.

Usage: python scripts/find_websites.py [--workers 32]
"""

from __future__ import annotations

import argparse
import csv
import re
import socket
import sys
from collections import defaultdict
import os
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pandas as pd  # noqa: E402

from hpthub import http, hpttxt  # noqa: E402
from hpthub.discover import _STOP, _norm_name, _tokens, clean_location, hospital_score  # noqa: E402
from hpthub.registry import REG_DIR, ROOT, is_vendor_host  # noqa: E402

DISC = ROOT / "data" / "discovery"
STATES = {"AL": "alabama", "AK": "alaska", "AZ": "arizona", "AR": "arkansas", "CA": "california", "CO": "colorado",
          "CT": "connecticut", "DE": "delaware", "DC": "district of columbia", "FL": "florida", "GA": "georgia",
          "HI": "hawaii", "ID": "idaho", "IL": "illinois", "IN": "indiana", "IA": "iowa", "KS": "kansas",
          "KY": "kentucky", "LA": "louisiana", "ME": "maine", "MD": "maryland", "MA": "massachusetts",
          "MI": "michigan", "MN": "minnesota", "MS": "mississippi", "MO": "missouri", "MT": "montana",
          "NE": "nebraska", "NV": "nevada", "NH": "new hampshire", "NJ": "new jersey", "NM": "new mexico",
          "NY": "new york", "NC": "north carolina", "ND": "north dakota", "OH": "ohio", "OK": "oklahoma",
          "OR": "oregon", "PA": "pennsylvania", "RI": "rhode island", "SC": "south carolina", "SD": "south dakota",
          "TN": "tennessee", "TX": "texas", "UT": "utah", "VT": "vermont", "VA": "virginia", "WA": "washington",
          "WV": "west virginia", "WI": "wisconsin", "WY": "wyoming", "PR": "puerto rico", "GU": "guam",
          "VI": "virgin islands", "MP": "northern mariana islands", "AS": "american samoa"}
_DROP = {"inc", "llc", "the", "corp", "corporation", "co", "lp", "pc", "dba", "of", "and"}
_SHORT = {"medical center": "mc", "health center": "hc", "regional medical center": "rmc",
          "memorial hospital": "mh", "community hospital": "ch", "county hospital": "ch",
          "regional hospital": "rh", "health system": "hs", "healthcare": "hc", "hospital": "hosp"}


def name_guesses(name: str, city: str) -> list[str]:
    n = _norm_name(name)
    n = re.sub(r"[^a-z0-9 ]", " ", n.replace("&", " and "))
    words = [w for w in n.split() if w not in _DROP]
    if not words:
        return []
    full = "".join(words)
    stems = [full]
    core = [w for w in words if w not in _STOP]
    if core and core != words:
        stems.append("".join(core))
        stems.append("".join(core) + "hospital")
        stems.append("".join(core) + "health")
    phrase = " ".join(words)
    for long, short in _SHORT.items():
        if phrase.endswith(long):
            stems.append(phrase[: -len(long)].replace(" ", "") + short)
    c = re.sub(r"[^a-z]", "", city.lower())
    if core and c and c not in "".join(core):
        stems.append("".join(core) + c)
    out = []
    for s in dict.fromkeys(stems):
        if 4 <= len(s) <= 40:
            out += [s + ".org", s + ".com"]
    return out


def resolves(domain: str) -> bool:
    for h in (domain, "www." + domain):
        try:
            socket.getaddrinfo(h, 443)
            return True
        except OSError:
            pass
    return False


def _get(sess, url: str, max_bytes: int):
    """fetch_text with a hard 30 s deadline, so a slow-drip server can't hold a worker."""
    one = ThreadPoolExecutor(max_workers=1)
    fut = one.submit(http.fetch_text, sess, url, max_bytes=max_bytes, timeout=15)
    try:
        return fut.result(timeout=30)
    except TimeoutError:
        return None
    finally:
        one.shutdown(wait=False)


def page_text(sess, domain: str) -> str:
    for h in ("www." + domain, domain):
        f = _get(sess, f"https://{h}/", 1_500_000)
        if f and f.status == 200 and f.body:
            t = http.decode_text(f.body)
            t = re.sub(r"<script.*?</script>|<style.*?</style>", " ", t, flags=re.S | re.I)
            t = re.sub(r"<[^>]+>", " ", t)
            t = re.sub(r"&nbsp;|&#160;", " ", t)
            return " ".join(t.lower().split())
    return ""


def address_shown(text: str, h) -> str:
    """Evidence that the page is about this hospital's location ('' if none)."""
    if not text:
        return ""
    z = str(h["zip"]).split("-")[0].split(".")[0].zfill(5)[:5]
    has_zip = bool(z) and re.search(rf"\b{z}\b", text) is not None
    num = re.match(r"\s*(\d+)", str(h["address"]))
    street = [w for w in re.sub(r"[^a-z ]", " ", str(h["address"]).lower()).split()
              if len(w) > 2 and w not in {"street", "avenue", "road", "drive", "highway", "blvd", "north", "south",
                                          "east", "west", "suite", "box"}]
    has_addr = bool(num) and re.search(rf"\b{num.group(1)}\b", text) is not None and \
        any(re.search(rf"\b{re.escape(w)}\b", text) for w in street[:2])
    city = " ".join(str(h["city"]).lower().split())
    st = STATES.get(h["state"], "")
    has_city = bool(city) and city in text and (st in text or re.search(rf"\b{h['state'].lower()}\b", text))
    if has_zip and has_addr:
        return f"home page shows {h['address']}, {z}"
    if has_zip and has_city:
        return f"home page shows {h['city']}, {h['state']} {z}"
    return ""


def name_shown(text: str, name: str) -> bool:
    core = _tokens(_norm_name(name)) - _STOP - _DROP
    return bool(core) and all(re.search(rf"\b{re.escape(t)}\b", text) for t in core)


def hpt_names(sess, domain: str, h) -> str:
    """Location name in the domain's live cms-hpt.txt that matches the hospital ('' if none)."""
    for host in ("www." + domain, domain):
        f = _get(sess, f"https://{host}/cms-hpt.txt", 5_000_000)
        if f and f.status == 200 and f.body and b"<html" not in f.body[:2000].lower():
            for e in hpttxt.parse(http.decode_text(f.body)).entries:
                if hospital_score(h["name"], h["city"], e.location_name) >= 0.8:
                    return e.location_name
            return ""
    return ""


def cross_matches(targets: pd.DataFrame) -> dict[str, list[tuple[str, str, float]]]:
    p = DISC / "hpt_txt_entries.csv"
    if not p.exists():
        return {}
    e = pd.read_csv(p, dtype=str).fillna("")
    e = e[(e["location_name"] != "") & ~e["domain"].map(is_vendor_host)].drop_duplicates(["domain", "location_name"])
    e = e.reset_index(drop=True)
    idx = defaultdict(set)
    for i, loc in enumerate(e["location_name"]):
        for t in _tokens(_norm_name(clean_location(loc))) - _STOP:
            idx[t].add(i)
    out = {}
    for _, h in targets.iterrows():
        toks = _tokens(_norm_name(h["name"])) - _STOP
        cand = set().union(*(idx[t] for t in toks)) if toks else set()
        sc = sorted(((hospital_score(h["name"], h["city"], e.at[i, "location_name"]), i) for i in cand), reverse=True)
        hits = [(e.at[i, "domain"], e.at[i, "location_name"], s) for s, i in sc[:5] if s >= 0.85]
        if hits:
            out[h["ccn"]] = list(dict.fromkeys(hits))
    return out


def check(sess, h, cross: list[tuple[str, str, float]], dom_states: dict[str, set]) -> dict | None:
    pages: dict[str, str] = {}

    def text(d):
        if d not in pages:
            pages[d] = page_text(sess, d)
        return pages[d]

    # 1. another site's cms-hpt.txt names the hospital: keep it if that site shows its address
    #    (a system's home page rarely shows each hospital's address, so an exact name
    #    match on a website already used by hospitals in the same state also counts)
    for d, loc, sc in cross:
        ev = address_shown(text(d), h)
        if not ev and sc >= 0.98 and h["state"] in dom_states.get(d, set()):
            ev = f"exact name match; {d} serves other {h['state']} hospitals"
        if ev:
            return {"domain": d, "method": "listed in another website's cms-hpt.txt",
                    "evidence": f'{d}/cms-hpt.txt lists "{loc}"; {ev}'}
    # 2. name guesses
    for d in name_guesses(h["name"], h["city"]):
        if is_vendor_host(d) or not resolves(d):
            continue
        t = text(d)
        ev = address_shown(t, h)
        if not ev:
            continue
        loc = hpt_names(sess, d, h)
        if loc:
            return {"domain": d, "method": "name guess", "evidence": f'{d}/cms-hpt.txt lists "{loc}"; {ev}'}
        # no cms-hpt.txt naming it: require the street address too (a county or city
        # website can show the same town and ZIP)
        if name_shown(t, h["name"]) and ev.startswith(f"home page shows {h['address']},"):
            return {"domain": d, "method": "name guess", "evidence": f"home page names the hospital; {ev}"}
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=32)
    ap.add_argument("--minutes", type=float, default=30, help="stop checking after this long")
    a = ap.parse_args()
    reg = pd.read_csv(REG_DIR / "hospitals.csv", dtype=str).fillna("")
    targets = reg[(reg["domains"] == "") & (reg["hpt_exempt"].str.lower() != "true")]
    print(f"{len(targets)} hospitals without a known website")
    cross = cross_matches(targets)
    print(f"{len(cross)} named in another website's cms-hpt.txt")
    dom_states: dict[str, set] = defaultdict(set)
    for ds, st in zip(reg["domains"], reg["state"]):
        for d in ds.split():
            dom_states[d].add(st)
    sess = http.session(retries=1)
    ex = ThreadPoolExecutor(max_workers=a.workers)
    futs = {ex.submit(check, sess, h, cross.get(h["ccn"], []), dom_states): h["ccn"] for _, h in targets.iterrows()}
    found, t0 = {}, time.time()
    try:
        for i, fut in enumerate(as_completed(futs, timeout=a.minutes * 60), 1):
            try:
                r = fut.result()
            except Exception as e:  # one bad website must not stop the run
                print(f"  {futs[fut]}: {e}")
                r = None
            if r:
                found[futs[fut]] = r
            if i % 50 == 0:
                print(f"  {i}/{len(futs)} hospitals checked, {len(found)} found ({time.time() - t0:.0f}s)", flush=True)
    except TimeoutError:
        print(f"time limit reached; {sum(f.done() for f in futs)}/{len(futs)} hospitals checked")
    ex.shutdown(wait=False, cancel_futures=True)
    print(f"{len(found)} websites found and verified")

    ev = REG_DIR / "inferred_websites.csv"
    names = reg.set_index("ccn")
    rows_ev = {}
    if ev.exists():  # keep earlier finds (those hospitals now have a website, so are not re-checked)
        with ev.open(newline="") as f:
            rows_ev = {r["ccn"]: r for r in csv.DictReader(f)}
    for c, r in found.items():
        rows_ev[c] = {"ccn": c, "name": names.at[c, "name"], "city": names.at[c, "city"],
                      "state": names.at[c, "state"], **r}
    with ev.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["ccn", "name", "city", "state", "domain", "method", "evidence"])
        w.writeheader()
        for c in sorted(rows_ev):
            w.writerow(rows_ev[c])
    p = REG_DIR / "domain_leads.csv"
    with p.open(newline="") as f:
        rows = {(r["ccn"], r["source"]): r for r in csv.DictReader(f)}
    for c, r in found.items():
        rows[(c, "inferred")] = {"ccn": c, "domains": r["domain"], "source": "inferred"}
    with p.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["ccn", "domains", "source"])
        w.writeheader()
        for k in sorted(rows):
            w.writerow(rows[k])
    print(f"{len(found)} inferred leads -> {p}, evidence -> {ev}", flush=True)
    os._exit(0)  # don't wait for checks still stuck on slow websites


if __name__ == "__main__":
    main()
