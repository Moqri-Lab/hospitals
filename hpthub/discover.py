"""Crawl ``https://<domain>/cms-hpt.txt`` for every registry domain and match
the listed locations back to registry hospitals (CCNs)."""

from __future__ import annotations

import datetime as dt
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher
from pathlib import Path

import pandas as pd

from . import http, hpttxt, registry

ROOT = registry.ROOT
DISC_DIR = ROOT / "data" / "discovery"

_STOP = {"the", "of", "and", "at", "inc", "llc", "hospital", "hospitals", "medical", "center",
         "centre", "health", "healthcare", "system", "campus", "regional", "memorial", "saint", "st",
         "mc", "ctr", "hosp", "general", "community", "a", "an", "for", "&"}


def _tokens(s: str) -> set[str]:
    s = re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())
    return {t for t in s.split() if t}


def name_score(a: str, b: str) -> float:
    """0..1 similarity tuned for hospital names (distinctive tokens weigh most)."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    da, db = ta - _STOP, tb - _STOP
    if da and db:
        overlap = len(da & db) / min(len(da), len(db))
    else:
        overlap = len(ta & tb) / min(len(ta), len(tb))
    seq = SequenceMatcher(None, " ".join(sorted(ta)), " ".join(sorted(tb))).ratio()
    return round(0.7 * overlap + 0.3 * seq, 3)


def fetch_domain(sess, domain: str) -> dict:
    url = f"https://{domain}/cms-hpt.txt"
    f = http.fetch_head(sess, url, max_bytes=5_000_000, timeout=25)
    rec = {
        "domain": domain, "url": url, "final_url": f.final_url, "http_status": f.status,
        "content_type": f.content_type, "bytes": len(f.body), "error": f.error,
        "n_entries": 0, "parse_issues": "", "found": False,
        "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    }
    entries = []
    if f.status == 200 and f.body:
        parsed = hpttxt.parse(http.decode_text(f.body))
        rec["parse_issues"] = "; ".join(parsed.issues)
        rec["n_entries"] = len(parsed.entries)
        rec["found"] = parsed.ok
        if parsed.ok:
            entries = [dict(domain=domain, **e.as_dict()) for e in parsed.entries]
    return {"domain": rec, "entries": entries}


def crawl(domains: list[str], workers: int = 16) -> tuple[pd.DataFrame, pd.DataFrame]:
    sess = http.session()
    dom_rows, entry_rows = [], []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fetch_domain, sess, d): d for d in domains}
        for i, fut in enumerate(as_completed(futs), 1):
            res = fut.result()
            dom_rows.append(res["domain"])
            entry_rows.extend(res["entries"])
            if i % 50 == 0:
                print(f"  {i}/{len(domains)} domains checked")
    return pd.DataFrame(dom_rows), pd.DataFrame(entry_rows)


def match(reg: pd.DataFrame, domains_df: pd.DataFrame, entries: pd.DataFrame,
          threshold: float = 0.55) -> pd.DataFrame:
    """For each hospital pick the best cms-hpt.txt entry on any of its domains."""
    found = set(domains_df.loc[domains_df["found"] == True, "domain"])  # noqa: E712
    by_domain = {d: g for d, g in entries.groupby("domain")} if len(entries) else {}
    out = []
    for _, h in reg.iterrows():
        doms = [d for d in h["domains"].split() if d]
        best, best_score, best_dom = None, -1.0, ""
        for d in doms:
            g = by_domain.get(d)
            if g is None:
                continue
            for _, e in g.iterrows():
                s = name_score(h["name"], e["location_name"])
                if s > best_score:
                    best, best_score, best_dom = e, s, d
        row = {"ccn": h["ccn"], "name": h["name"], "state": h["state"], "beds": h["beds"],
               "hpt_txt_domain": best_dom, "hpt_txt_found": any(d in found for d in doms),
               "location_name": "", "mrf_url": "", "source_page_url": "", "match_score": None,
               "match_status": "no-domain" if not doms else "no-hpt-txt"}
        if best is not None:
            row.update(location_name=best["location_name"], mrf_url=best["mrf_url"],
                       source_page_url=best["source_page_url"], match_score=best_score,
                       match_status="matched" if best_score >= threshold else "low-confidence")
        out.append(row)
    return pd.DataFrame(out)


def run(reg_path: Path | None = None, limit: int | None = None, workers: int = 16) -> dict[str, Path]:
    reg = registry.load(reg_path)
    if limit:
        reg = reg.head(limit)
    domains: list[str] = []
    for ds in reg["domains"]:
        for d in ds.split():
            if d not in domains:
                domains.append(d)
    print(f"checking {len(domains)} domains for {len(reg)} hospitals")
    dom_df, ent_df = crawl(domains, workers=workers)
    if ent_df.empty:
        ent_df = pd.DataFrame(columns=["domain", "location_name", "source_page_url", "mrf_url",
                                       "contact_name", "contact_email", "issues"])
    m = match(reg, dom_df, ent_df)
    DISC_DIR.mkdir(parents=True, exist_ok=True)
    paths = {
        "domains": DISC_DIR / "domains.csv",
        "entries": DISC_DIR / "hpt_txt_entries.csv",
        "hospital_mrfs": DISC_DIR / "hospital_mrfs.csv",
    }
    dom_df.sort_values("domain").to_csv(paths["domains"], index=False)
    ent_df.drop(columns=["contact_email"], errors="ignore").sort_values(["domain", "location_name"]) \
        .to_csv(paths["entries"], index=False)
    m.to_csv(paths["hospital_mrfs"], index=False)
    print(m["match_status"].value_counts().to_string())
    return paths
