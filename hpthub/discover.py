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
# Snapshots of cms-hpt.txt files fetched elsewhere (e.g. from a residential
# connection when a site blocks cloud IPs). Used when the live fetch fails.
SNAPSHOT_DIR = DISC_DIR / "hpt_txt_snapshots"

_STOP = {"the", "of", "and", "at", "inc", "llc", "hospital", "hospitals", "medical", "center",
         "centre", "health", "healthcare", "system", "campus", "regional", "memorial", "saint", "st",
         "mc", "ctr", "hosp", "general", "community", "a", "an", "for", "&"}


def _tokens(s: str) -> set[str]:
    s = re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())
    return {t for t in s.split() if t}


_ABBREV = [
    (r"\buh\b", "university hospitals"), (r"\bo u\b", "ou"), (r"\bunc\b", "unc"),
    (r"\buofl\b", "uofl"), (r"\bu of\b", "university of"), (r"\buniv\.?\b", "university"),
    (r"\bmed ctr\b", "medical center"), (r"\bctr\b", "center"), (r"\bhosp\b", "hospital"),
    (r"\bst\.?\s", "saint "), (r"\bmt\.?\s", "mount "), (r"\bchildren'?s\b", "childrens"),
    (r",?\s*(the|inc\.?|llc)$", ""), (r"^the\s", ""),
]


def clean_location(name: str) -> str:
    """Drop parenthetical notes and 'a facility of ...' suffixes used in cms-hpt.txt names."""
    n = re.sub(r"\(.*?\)", " ", name or "")
    n = re.sub(r",?\s+a (facility|department|campus) of .*$", "", n, flags=re.I)
    return re.sub(r"\s+-\s*$", "", n).strip()


def _norm_name(n: str) -> str:
    n = (n or "").lower().strip()
    for rx, rep in _ABBREV:
        n = re.sub(rx, rep, n)
    return n


def name_score(a: str, b: str) -> float:
    """Best of the raw score and the score after cleaning/abbreviation expansion."""
    raw = _name_score(a, b)
    norm = _name_score(_norm_name(a), _norm_name(clean_location(b)))
    return max(raw, norm)


def _name_score(a: str, b: str) -> float:
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


def access_issue(status, error: str, body: bytes = b"") -> str:
    """Why a live cms-hpt.txt could not be read, in plain words ('' if it could)."""
    e = error or ""
    if status == 200 and body and b"<html" not in body[:2000].lower():
        return ""
    if status == 403:
        return "refused automated access (HTTP 403)"
    if status == 429 or "429" in e:
        return "rate-limited automated access (HTTP 429)"
    if status == 404:
        return "no cms-hpt.txt (HTTP 404)"
    if status == 200:
        return "returned a web page instead of a text file"
    if "SSL" in e:
        return "TLS certificate error"
    if "NameResolution" in e or "Name or service" in e:
        return "website address does not resolve"
    if "Timeout" in e or "timed out" in e:
        return "timed out"
    if status:
        return f"HTTP {status}"
    return "connection failed" if e else ""


def fetch_archive(sess, host: str):
    """Latest Internet Archive (Wayback Machine) copy of https://<host>/cms-hpt.txt.

    A public archive copy, used only when the live file cannot be read. Returns
    (body, archived_url, YYYY-MM-DD) or None."""
    try:
        r = sess.get("https://archive.org/wayback/available",
                     params={"url": f"{host}/cms-hpt.txt"}, timeout=30)
        snap = r.json().get("archived_snapshots", {}).get("closest") or {}
    except Exception:  # noqa: BLE001
        return None
    if not snap.get("available") or str(snap.get("status")) != "200":
        return None
    ts = snap["timestamp"]
    raw = f"https://web.archive.org/web/{ts}id_/https://{host}/cms-hpt.txt"
    f = http.fetch_head(sess, raw, max_bytes=5_000_000, timeout=60)
    if f.status != 200 or not f.body or b"<html" in f.body[:2000].lower():
        return None
    return f.body, f"https://web.archive.org/web/{ts}/https://{host}/cms-hpt.txt", f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}"


_CC_INDEXES: list[str] = []


def fetch_commoncrawl(sess, host: str, n_indexes: int = 4):
    """Latest Common Crawl capture of https://<host>/cms-hpt.txt (public web archive).

    Looks the URL up in the newest crawl indexes and reads just that one WARC
    record with an HTTP Range request. Returns (body, record_url, YYYY-MM-DD) or None."""
    import gzip
    import json as _json
    try:
        if not _CC_INDEXES:
            _CC_INDEXES.extend(c["cdx-api"] for c in sess.get("https://index.commoncrawl.org/collinfo.json",
                                                              timeout=30).json())
        for api in _CC_INDEXES[:n_indexes]:
            r = sess.get(api, params={"url": f"{host}/cms-hpt.txt", "output": "json"}, timeout=60)
            if r.status_code != 200 or not r.text.strip():
                continue
            caps = [_json.loads(x) for x in r.text.strip().splitlines()]
            caps = [c for c in caps if str(c.get("status")) == "200"]
            if not caps:
                continue
            c = max(caps, key=lambda c: c["timestamp"])
            off, ln = int(c["offset"]), int(c["length"])
            w = sess.get(f"https://data.commoncrawl.org/{c['filename']}",
                         headers={"Range": f"bytes={off}-{off + ln - 1}"}, timeout=60)
            rec = gzip.decompress(w.content)
            # WARC headers, blank line, HTTP headers, blank line, body
            parts = rec.split(b"\r\n\r\n", 2)
            body = parts[2] if len(parts) == 3 else b""
            if not body or b"<html" in body[:2000].lower():
                continue
            ts = c["timestamp"]
            return body, f"{api}?url={host}/cms-hpt.txt", f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}"
    except Exception:  # noqa: BLE001
        return None
    return None


def fetch_domain(sess, domain: str, archive: bool = True) -> dict:
    """Fetch cms-hpt.txt from the domain root, trying both the bare and www. host.

    If the live file can't be read, fall back to a committed saved copy, then to
    the Internet Archive's latest copy. The reason the live fetch failed is kept
    in ``access_issue`` either way."""
    hosts = [domain] + ([f"www.{domain}"] if not domain.startswith("www.") else [domain[4:]])
    f, url, attempts = None, "", []
    for host in hosts:
        url = f"https://{host}/cms-hpt.txt"
        f = http.fetch_head(sess, url, max_bytes=5_000_000, timeout=20)
        if f.error and ("Timeout" in f.error or "timed out" in f.error):
            f = http.fetch_head(sess, url, max_bytes=5_000_000, timeout=90)  # slow sites
        attempts.append(access_issue(f.status, f.error, f.body))
        if not attempts[-1]:
            break
    issue = attempts[-1] if all(attempts) else ""
    # report the most informative failure (a 403 on www beats a DNS miss on the bare host)
    for a in attempts:
        if a.startswith(("refused", "rate-limited", "TLS", "timed out")):
            issue = a
            break
    rec = {
        "domain": domain, "url": url, "final_url": f.final_url, "http_status": f.status,
        "content_type": f.content_type, "bytes": len(f.body), "error": f.error,
        "access_issue": issue, "source": "live" if not issue else "",
        "n_entries": 0, "parse_issues": "", "found": False,
        "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    }
    if issue:
        snap = SNAPSHOT_DIR / f"{domain}.txt"
        arc = None
        if snap.exists():
            f.body, f.status = snap.read_bytes(), 200
            rec.update(source="saved copy", url=f"snapshot:{snap.name}")
        elif archive:
            for host in hosts:
                arc = fetch_archive(sess, host)
                if arc:
                    break
            if arc:
                f.body, f.status = arc[0], 200
                rec.update(source=f"Internet Archive {arc[2]}", url=arc[1])
            else:
                for host in hosts:
                    arc = fetch_commoncrawl(sess, host)
                    if arc:
                        break
                if arc:
                    f.body, f.status = arc[0], 200
                    rec.update(source=f"Common Crawl {arc[2]}", url=arc[1])
        rec["bytes"] = len(f.body)
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


def load_location_overrides() -> dict[str, list[str]]:
    """Hand-reviewed CCN -> exact cms-hpt.txt location names ('' = hospital not listed)."""
    import csv as _csv
    p = registry.REG_DIR / "location_overrides.csv"
    out: dict[str, list[str]] = {}
    if p.exists():
        with p.open(newline="", encoding="utf-8") as f:
            for r in _csv.DictReader(f):
                out.setdefault(r["ccn"].zfill(6), []).append(r["location_name"].strip())
    return out


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
    blocked = set()
    if len(domains_df):
        st = domains_df.get("http_status", pd.Series(dtype=str)).astype(str)
        err = domains_df.get("error", pd.Series(dtype=str)).fillna("").astype(str)
        iss = domains_df.get("access_issue", pd.Series("", index=domains_df.index)).fillna("").astype(str)
        unreachable = (st.str.startswith(("403", "429")) | (err != "")
                       | ((iss != "") & ~iss.str.startswith("no cms-hpt.txt")))
        bad = (domains_df["found"].astype(str) != "True") & unreachable
        blocked = set(domains_df.loc[bad, "domain"])
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

    overrides = load_location_overrides()
    out = []
    for _, h in reg.iterrows():
        doms = [d for d in str(h["domains"]).split() if d]
        base = {"ccn": h["ccn"], "hpt_txt_found": any(d in found for d in doms)}
        if h["ccn"] in overrides:
            wanted = {n.lower() for n in overrides[h["ccn"]] if n}
            hits = [(d, e) for d in doms for _, e in by_domain.get(d, pd.DataFrame()).iterrows()
                    if str(e["location_name"]).strip().lower() in wanted]
            if not wanted or hits:
                if not hits:
                    out.append({**base, "hpt_txt_domain": doms[0] if doms else "", "match_score": None,
                                "match_status": "not-listed", **{f: "" for f in ENTRY_FIELDS}})
                    continue
                by_url: dict[str, list] = {}
                for d, e in hits:
                    by_url.setdefault(e["mrf_url"], []).append((d, e))
                for url, group in by_url.items():
                    d, e = group[0]
                    row = {**base, "hpt_txt_domain": d, "match_score": 1.0, "match_status": "matched-manual",
                           **{f: e.get(f, "") for f in ENTRY_FIELDS}}
                    row["location_name"] = "; ".join(dict.fromkeys(g[1]["location_name"] for g in group))
                    out.append(row)
                continue
        scored = []
        for d in doms:
            g = by_domain.get(d)
            if g is None:
                continue
            for _, e in g.iterrows():
                scored.append((name_score(h["name"], e["location_name"]), d, e))
        if not scored:
            tried = [d for d in doms if d in blocked] or doms
            out.append({**base, "hpt_txt_domain": tried[0] if tried else "", "match_score": None,
                        "match_status": ("no-domain" if not doms else
                                         "blocked" if any(d in blocked for d in doms) else "no-hpt-txt"),
                        **{f: "" for f in ENTRY_FIELDS}})
            continue
        scored.sort(key=lambda t: -t[0])
        good = [t for t in scored if t[0] >= threshold
                and t[0] >= owner_score(t[1], t[2]["location_name"], h["ccn"])]
        if good:
            # keep entries close to the best one (or sharing its price file)
            top = good[0][0]
            good = [t for t in good if t[0] >= top - 0.12 or t[2]["mrf_url"] == good[0][2]["mrf_url"]]
        status = "matched"
        if not good:
            # If this CCN is the only registry hospital using the domain, every
            # entry on it that no other hospital claims belongs to it (e.g. a
            # system filing under one CCN, or a renamed hospital).
            sole = [t for t in scored if len([c for c, _ in rivals.get(t[1], [])]) <= 1]
            if sole:
                good, status = sole, "matched-sole"
        chosen = good or scored[:1]
        by_url: dict[str, list] = {}
        for sc, d, e in chosen:
            by_url.setdefault(e["mrf_url"], []).append((sc, d, e))
        for url, group in by_url.items():
            sc, d, e = group[0]
            row = {**base, "hpt_txt_domain": d, "match_score": sc,
                   "match_status": status if good else "low-confidence",
                   **{f: e.get(f, "") for f in ENTRY_FIELDS}}
            # several locations can share one price file; list them all
            row["location_name"] = "; ".join(dict.fromkeys(g[2]["location_name"] for g in group))
            out.append(row)
    return pd.DataFrame(out)


def plausible_size(n, content_type: str = "") -> bool:
    """A HEAD Content-Length is trusted only if it looks like a real price file:
    some servers answer HEAD with an HTML page or a tiny placeholder length."""
    if n is None or n != n:  # None or NaN
        return False
    return int(n) >= 1024 and "html" not in (content_type or "").lower()


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
        if not plausible_size(rec["mrf_bytes"], rec["mrf_content_type"]):
            rec["mrf_bytes"] = None
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
