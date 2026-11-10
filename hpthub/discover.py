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
    """0..1 similarity tuned for hospital names (distinctive tokens weigh most).

    Tokens are matched against the other name with spaces removed too, so
    "New York-Presbyterian" matches "NewYork-Presbyterian".
    """
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    sa = re.sub(r"[^a-z0-9]", "", (a or "").lower())
    sb = re.sub(r"[^a-z0-9]", "", (b or "").lower())
    da, db = (ta - _STOP) or ta, (tb - _STOP) or tb
    hit_a = sum(1 for t in da if t in tb or (len(t) >= 3 and t in sb)) / len(da)
    hit_b = sum(1 for t in db if t in ta or (len(t) >= 3 and t in sa)) / len(db)
    overlap = (hit_a + hit_b) / 2
    seq = SequenceMatcher(None, " ".join(sorted(ta)), " ".join(sorted(tb))).ratio()
    return round(0.7 * overlap + 0.3 * seq, 3)


def fetch_domain(sess, domain: str) -> dict:
    """Fetch cms-hpt.txt from the domain root, trying both the bare and www. host."""
    hosts = [domain] + ([f"www.{domain}"] if not domain.startswith("www.") else [domain[4:]])
    f, url = None, ""
    for host in hosts:
        url = f"https://{host}/cms-hpt.txt"
        f = http.fetch_head(sess, url, max_bytes=5_000_000, timeout=20)
        if f.status == 200 and f.body and "<html" not in f.body[:2000].decode("latin-1").lower():
            break
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


ENTRY_FIELDS = ["location_name", "source_page_url", "mrf_url", "contact_name", "contact_email",
                "issues"]


def match(reg: pd.DataFrame, domains_df: pd.DataFrame, entries: pd.DataFrame,
          threshold: float = 0.55, universe: pd.DataFrame | None = None) -> pd.DataFrame:
    """One row per (hospital, cms-hpt.txt entry) that names this hospital.

    Every entry on the hospital's domains scoring >= ``threshold`` is kept (a
    hospital can publish several price files). If none passes, the single best
    entry is kept as ``low-confidence`` so a human can check it.
    """
    found = set(domains_df.loc[domains_df["found"].astype(str) == "True", "domain"]) if len(domains_df) else set()
    by_domain = {d: g for d, g in entries.groupby("domain")} if len(entries) else {}
    # Other registry hospitals on the same domain compete for each entry, so a
    # system's file for "Hospital B" isn't attached to "Hospital A".
    universe = reg if universe is None else universe
    rivals: dict[str, list[tuple[str, str]]] = {}
    for _, u in universe.iterrows():
        for d in str(u["domains"]).split():
            rivals.setdefault(d, []).append((u["ccn"], u["name"]))
    best_owner: dict[tuple[str, str], float] = {}

    def owner_score(d: str, loc: str, ccn: str) -> float:
        key = (d, loc, ccn)
        if key not in best_owner:
            best_owner[key] = max((name_score(n, loc) for c, n in rivals.get(d, []) if c != ccn), default=0.0)
        return best_owner[key]

    out = []
    for _, h in reg.iterrows():
        doms = [d for d in str(h["domains"]).split() if d]
        base = {"ccn": h["ccn"], "hpt_txt_found": any(d in found for d in doms)}
        scored = []
        for d in doms:
            g = by_domain.get(d)
            if g is None:
                continue
            for _, e in g.iterrows():
                scored.append((name_score(h["name"], e["location_name"]), d, e))
        if not scored:
            out.append({**base, "hpt_txt_domain": "", "match_score": None,
                        "match_status": "no-domain" if not doms else "no-hpt-txt",
                        **{f: "" for f in ENTRY_FIELDS}})
            continue
        scored.sort(key=lambda t: -t[0])
        good = [t for t in scored if t[0] >= threshold
                and t[0] >= owner_score(t[1], t[2]["location_name"], h["ccn"])]
        seen = set()
        for sc, d, e in (good or scored[:1]):
            if e["mrf_url"] in seen:
                continue
            seen.add(e["mrf_url"])
            out.append({**base, "hpt_txt_domain": d, "match_score": sc,
                        "match_status": "matched" if good else "low-confidence",
                        **{f: e.get(f, "") for f in ENTRY_FIELDS}})
    return pd.DataFrame(out)


def head_mrf(sess, url: str) -> dict:
    """Metadata for an MRF URL via HTTP HEAD only (the price data itself is never downloaded)."""
    rec = {"mrf_url": url, "mrf_http_status": None, "mrf_content_type": "", "mrf_bytes": None,
           "mrf_last_modified": "", "mrf_final_url": "", "mrf_error": ""}
    try:
        r = sess.head(url, allow_redirects=True, timeout=30)
        cl = r.headers.get("Content-Length")
        rec.update(mrf_http_status=r.status_code, mrf_content_type=r.headers.get("Content-Type", ""),
                   mrf_bytes=int(cl) if cl and cl.isdigit() else None,
                   mrf_last_modified=r.headers.get("Last-Modified", ""), mrf_final_url=r.url)
    except Exception as e:  # noqa: BLE001
        rec["mrf_error"] = f"{type(e).__name__}: {str(e)[:150]}"
    return rec


_FORMATS = (("json", "json"), ("csv", "csv"), ("zip", "zip"), ("xlsx", "xlsx"), ("xls", "xls"),
            ("xml", "xml"), ("txt", "txt"), ("gz", "gzip"))


def mrf_format(url: str, content_type: str = "") -> str:
    """Best guess of the price file's format from its URL extension, else its Content-Type."""
    from urllib.parse import unquote, urlparse
    path = unquote(urlparse(url).path).lower()
    for ext, fmt in _FORMATS:
        if path.endswith("." + ext) or path.endswith(f".{ext}.gz"):
            return fmt
    ct = (content_type or "").lower()
    for key, fmt in (("json", "json"), ("csv", "csv"), ("zip", "zip"), ("spreadsheet", "xlsx"),
                     ("excel", "xls"), ("xml", "xml"), ("html", "html"), ("text/plain", "txt")):
        if key in ct:
            return fmt
    return ""


def head_all(urls: list[str], workers: int = 8) -> pd.DataFrame:
    sess = http.session()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return pd.DataFrame(list(ex.map(lambda u: head_mrf(sess, u), urls)))


def run(reg_path: Path | None = None, limit: int | None = None, workers: int = 16,
        head: bool = True) -> dict[str, Path]:
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
    m = match(reg, dom_df, ent_df, universe=registry.load())
    if head:
        urls = sorted({u for u in m["mrf_url"] if isinstance(u, str) and u.startswith("http")})
        if urls:
            m = m.merge(head_all(urls), on="mrf_url", how="left")
        else:
            for c in ("mrf_http_status", "mrf_content_type", "mrf_bytes", "mrf_last_modified",
                      "mrf_final_url", "mrf_error"):
                m[c] = None
        m["mrf_format"] = [mrf_format(u, ct) if isinstance(u, str) and u else ""
                           for u, ct in zip(m["mrf_url"], m["mrf_content_type"].fillna(""))]
    DISC_DIR.mkdir(parents=True, exist_ok=True)
    paths = {
        "domains": DISC_DIR / "domains.csv",
        "entries": DISC_DIR / "hpt_txt_entries.csv",
        "hospital_mrfs": DISC_DIR / "hospital_mrfs.csv",
    }
    dom_df.sort_values("domain").to_csv(paths["domains"], index=False)
    ent_df.sort_values(["domain", "location_name"]).to_csv(paths["entries"], index=False)
    m.to_csv(paths["hospital_mrfs"], index=False)
    print(m["match_status"].value_counts().to_string())
    return paths
