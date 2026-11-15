"""Build the catalog: one row per (hospital, price file).

Outputs
- ``data/catalog.csv``: full catalog (every registry hospital; crawled ones have
  their cms-hpt.txt fields and price-file link, format, size and date)
- ``CATALOG.md``: the crawled hospitals as a Markdown table, readable on GitHub
- ``data.json``: data for the browsable table in ``index.html``
"""

from __future__ import annotations

import datetime as dt
import re
from urllib.parse import urlparse
import json
from email.utils import parsedate_to_datetime
from pathlib import Path

import pandas as pd

from . import ein as ein_mod
from . import registry
from .discover import plausible_size

ROOT = registry.ROOT

CATALOG_COLUMNS = [
    "ccn", "name", "pos_name", "address", "city", "state", "zip", "facility_type", "ownership", "beds", "n_campuses", "system",
    "hpt_exempt", "exempt_note", "web_claude", "web_system_rule", "web_wikidata", "wikidata_id", "web_tpafs", "web_sutton", "web_osm", "osm_id", "web_inferred", "web_llm", "hpt_txt_domain", "hpt_txt_found", "hpt_txt_source", "hpt_txt_access_issue", "location_name", "source_page_url", "mrf_url",
    "hpt_txt_url", "mrf_format", "mrf_size_mb", "mrf_last_modified", "mrf_http_status", "contact_name",
    "contact_email", "ein", "npi", "match_status", "match_score", "confidence", "confidence_level", "txt_check", "txt_check_reason", "checked_at",
]


def _read(p: Path) -> pd.DataFrame:
    return pd.read_csv(p, dtype=str, keep_default_na=False) if p.exists() else pd.DataFrame()


def _date(v: str) -> str:
    try:
        return parsedate_to_datetime(v).date().isoformat() if v else ""
    except (TypeError, ValueError):
        return v


def _mb(v: str) -> str:
    try:
        return f"{int(float(v)) / 1e6:.1f}" if v not in ("", None) else ""
    except ValueError:
        return ""


def catalog() -> pd.DataFrame:
    reg = registry.load()
    mrfs = _read(ROOT / "data" / "discovery" / "hospital_mrfs.csv")
    if not mrfs.empty:
        mrfs = mrfs.drop(columns=[c for c in ("name", "state", "beds") if c in mrfs.columns])
        reg = reg.merge(mrfs, on="ccn", how="left")
    reg = reg.fillna("")
    doms = _read(ROOT / "data" / "discovery" / "domains.csv")
    if not doms.empty and "hpt_txt_domain" in reg.columns:
        d = doms.set_index("domain")
        src = d["source"] if "source" in d else d["url"].map(lambda u: "saved copy" if str(u).startswith("snapshot:") else "live")
        iss = d["access_issue"] if "access_issue" in d else pd.Series("", index=d.index)
        reg["hpt_txt_source"] = reg["hpt_txt_domain"].map(src).fillna("")
        reg["hpt_txt_access_issue"] = reg["hpt_txt_domain"].map(iss).fillna("")
        # link to the file actually read: after redirects for live files (a bare-domain
        # link can land on the home page), the archive or snapshot URL otherwise
        fin = d["final_url"].fillna("") if "final_url" in d else pd.Series("", index=d.index)
        live_url = [f if (s in ("live", "") and str(f).startswith("http")) else u
                    for s, f, u in zip(src, fin, d["url"])]
        reg["hpt_txt_url"] = reg["hpt_txt_domain"].map(pd.Series(live_url, index=d.index)).fillna("")
    for c in CATALOG_COLUMNS:
        if c not in reg.columns:
            reg[c] = ""
    # campuses = distinct cms-hpt.txt location names attached to the CCN
    locs = reg[reg["location_name"] != ""].groupby("ccn")["location_name"].apply(
        lambda s: len({x.strip() for v in s for x in v.split(";") if x.strip()}))
    reg["n_campuses"] = reg["ccn"].map(locs).fillna("").map(lambda v: str(int(v)) if v != "" else "")
    if "mrf_bytes" in reg.columns:
        ok = [plausible_size(float(b) if b not in ("", None) else None, ct)
              for b, ct in zip(reg["mrf_bytes"], reg.get("mrf_content_type", [""] * len(reg)))]
        reg["mrf_size_mb"] = [(_mb(b) if k else "") for b, k in zip(reg["mrf_bytes"], ok)]
    else:
        reg["mrf_size_mb"] = ""
    reg["mrf_last_modified"] = reg["mrf_last_modified"].map(_date)
    # EIN from the price-file name: in the link, else in the file name the server gives
    # (Content-Disposition, from the HEAD request), else in the link after redirects.
    fns = reg["mrf_filename"] if "mrf_filename" in reg.columns else pd.Series("", index=reg.index)
    fins = reg["mrf_final_url"] if "mrf_final_url" in reg.columns else pd.Series("", index=reg.index)
    parsed = [ein_mod.from_parts(u, fn, fu) for u, fn, fu in zip(reg["mrf_url"], fns, fins)]
    # some hospitals put the EIN at the start of the location name ("26-1214372 NEA Baptist")
    lead = reg["location_name"].str.extract(r"^\s*(\d{2})-?(\d{7})(?!\d)", expand=True).fillna("")
    parsed = [p if p[0] or not a else (f"{a}-{b}", p[1]) for p, a, b in zip(parsed, lead[0], lead[1])]
    reg["ein"] = [p[0] for p in parsed]
    reg["npi"] = [p[1] for p in parsed]
    conf = reg.apply(confidence, axis=1, result_type="expand") if len(reg) else None
    if conf is not None:
        reg["confidence"], reg["confidence_level"] = conf[0], conf[1]
    dead = dead_hosts(reg)
    chk = {c: txt_check(g, dead) for c, g in reg[reg["match_status"] != ""].groupby("ccn")}
    reg["txt_check"] = reg["ccn"].map(lambda c: chk.get(c, ("", ""))[0])
    reg["txt_check_reason"] = reg["ccn"].map(lambda c: chk.get(c, ("", ""))[1])
    reg["checked_at"] = dt.date.today().isoformat()
    reg.loc[reg["match_status"] == "", "checked_at"] = ""
    return reg[CATALOG_COLUMNS]


WEB_SOURCE_COLS = ("web_system_rule", "web_wikidata", "web_tpafs", "web_sutton", "web_osm", "web_inferred", "web_llm", "web_claude")


def confidence(r) -> tuple[str, str]:
    """0-100 confidence that a price file belongs to this CMS hospital.

    base, by how the cms-hpt.txt entry was matched:
      matched         name similarity x 100 (similarity >= 0.55)
      matched-manual  85 (entry chosen on review)
      matched-city    70 (only entry naming the hospital's city)
      matched-sole    65 (only registry hospital on that website)
      low-confidence  name similarity x 60
    +5 for each additional independent source that gives the same website (max +15)
    -10 if the cms-hpt.txt came from a public archive, -5 if from a saved copy
    Levels: high >= 80, medium 60-79, low < 60.
    """
    st = r["match_status"]
    if not st.startswith("matched") and st != "low-confidence":
        return "", ""
    try:
        sc = float(r["match_score"])
    except (TypeError, ValueError):
        sc = 0.0
    base = {"matched": sc * 100, "matched-manual": 85, "matched-city": 70,
            "matched-sole": 65}.get(st, sc * 60)
    dom = r["hpt_txt_domain"]
    agree = sum(1 for c in WEB_SOURCE_COLS if dom and dom in str(r.get(c, "")).split())
    base += 5 * min(3, max(0, agree - 1))
    src = str(r.get("hpt_txt_source", ""))
    if src.startswith(("Internet Archive", "Common Crawl")):
        base -= 10
    elif src == "saved copy":
        base -= 5
    v = int(round(max(0, min(100, base))))
    return str(v), "high" if v >= 80 else "medium" if v >= 60 else "low"


def dead_hosts(df: pd.DataFrame, min_files: int = 5) -> set[str]:
    """Price-file servers that answered HTTP 404 to the HEAD request for every one of
    at least ``min_files`` different files. A whole server "missing" every file more
    likely means it does not answer HEAD requests, so these are not counted as failures."""
    f = df[df.get("mrf_url", pd.Series("", index=df.index)).astype(str) != ""].drop_duplicates("mrf_url")
    if not len(f):
        return set()
    host = f["mrf_url"].map(lambda u: urlparse(u).netloc.lower())
    code = f["mrf_http_status"].astype(str).str.split(".").str[0]
    out = set()
    for h, c in code.groupby(host):
        if len(c) >= min_files and c.isin(["404", "410"]).all():
            out.add(h)
    return out


# Bytes one negotiated (payer-specific) price takes up in a CMS-template price file,
# (low, typical, high). CSV "tall" rows hold one payer/plan rate each (~200-400 bytes);
# "wide" CSVs share the item columns across payers (~100 bytes per rate). JSON repeats
# field names per payer (~200-500). Zip/gzip compress CSV/JSON roughly 10:1.
BYTES_PER_PRICE = {"csv": (120, 220, 400), "json": (180, 300, 500), "zip": (12, 22, 40),
                   "gz": (12, 22, 40), "xlsx": (15, 30, 60)}


def price_totals(cat: pd.DataFrame) -> dict:
    """Total price files and an estimate of the negotiated prices in them, from the
    file sizes reported by HTTP HEAD requests only (files are never downloaded).
    Files without a reported size get the median size of their format."""
    f = cat[cat["mrf_url"].astype(str) != ""].drop_duplicates("mrf_url")
    size = pd.to_numeric(f["mrf_size_mb"], errors="coerce")
    fmt = f["mrf_format"].astype(str).str.lower()
    est = {"low": 0.0, "mid": 0.0, "high": 0.0}
    total_mb = 0.0
    for k, (lo, mid, hi) in BYTES_PER_PRICE.items():
        sz = size[fmt == k]
        if not len(sz):
            continue
        mb = sz.fillna(sz.median() if sz.notna().any() else 0).sum()
        total_mb += mb
        for key, b in (("low", hi), ("mid", mid), ("high", lo)):
            est[key] += mb * 1e6 / b
    return {"price_files": int(len(f)), "hospitals_with_files": int(cat.loc[cat["mrf_url"].astype(str) != "", "ccn"].nunique()),
            "sized_files": int(size.notna().sum()), "total_gb": round(total_mb / 1000, 1),
            "est_prices_low": int(est["low"]), "est_prices": int(est["mid"]), "est_prices_high": int(est["high"])}


def txt_check(g: pd.DataFrame, dead: set[str] = frozenset()) -> tuple[str, str]:
    """Hospital-level check of the cms-hpt.txt requirement (45 CFR 180.50(d)(6)).

    Only checks that a machine-readable cms-hpt.txt is publicly reachable at the
    website root and lists this hospital; it says nothing about price-file content.
    pass: a live cms-hpt.txt lists the hospital (confidence >= 60)
    blocked: the site refused automated access (the file may still open in a browser)
    fail: no cms-hpt.txt at the website root, or its file does not list the hospital
    unknown: no website known, uncertain match, or a network problem on our side
    """
    r = g.iloc[0]
    st = set(g["match_status"])
    if not any(st):
        return "", ""
    src = str(r.get("hpt_txt_source", ""))
    iss = str(r.get("hpt_txt_access_issue", ""))
    conf = pd.to_numeric(g["confidence"], errors="coerce").max()
    matched = any(x.startswith("matched") for x in st)
    dom = r["hpt_txt_domain"]
    # how many independent sources give the website we checked; a failure is only
    # blamed on the hospital when we are sure the website is really theirs
    agree = sum(1 for c in WEB_SOURCE_COLS if dom and dom in str(r.get(c, "")).split())
    sure = matched or agree >= 2
    files = g[g["mrf_url"] != ""]
    codes = [str(x).split(".")[0] for x in files.get("mrf_http_status", [])]
    if matched and len(files) and codes and all(c in ("404", "410") for c in codes):
        # A file URL (…/x.csv) answering HEAD with 404 is missing. An API-style URL
        # without a file name may only answer GET, which we never send.
        is_file = any(re.search(r"\.(csv|json|zip|xlsx?|xml|txt|gz)$", urlparse(u).path, re.I)
                      for u in files["mrf_url"])
        hosts = {urlparse(u).netloc.lower() for u in files["mrf_url"]}
        if is_file and hosts <= set(dead):
            return "unknown", (f"price file link answered HTTP {codes[0]} to a HEAD request, as did every other "
                               "price file on that server; the server may not answer HEAD requests")
        if is_file:
            return "fail", (f"its cms-hpt.txt lists this hospital, but the price file link returns HTTP {codes[0]} "
                            "(checked with an HTTP HEAD request)")
        return "unknown", (f"price file link answered HTTP {codes[0]} to a HEAD request; it is an API link that may "
                           "only answer downloads, which this catalog never makes")
    if matched and src in ("live", "") and not iss and conf >= 60:
        return "pass", f"listed in {dom}/cms-hpt.txt"
    bad_access = iss.startswith(("refused", "rate-limited", "returned a web page"))
    no_txt = "no-hpt-txt" in st or iss.startswith("no cms-hpt.txt")
    if (bad_access or no_txt) and not sure:
        return "unknown", f"{dom}: {iss or 'no cms-hpt.txt'}; not sure this is the hospital's website"
    if bad_access:  # reported separately: the file may well open in a browser
        code = re.search(r"HTTP \d+", iss)
        return "blocked", f"{dom} blocks automated requests" + (f" ({code.group(0)})" if code else "")
    if no_txt:
        return "fail", f"no cms-hpt.txt at the root of {dom}"
    if "not-listed" in st:
        return "fail", "the website's cms-hpt.txt does not list this hospital"
    if "no-domain" in st:
        return "unknown", "hospital website not known yet"
    if "low-confidence" in st and not matched:
        return "unknown", "uncertain match; needs review"
    if iss:
        return "unknown", f"could not check: {iss}"
    return "unknown", "could not check"


def _md_link(url: str, text: str) -> str:
    return f"[{text}]({url})" if url else ""


def ein_crosswalk(cat: pd.DataFrame) -> pd.DataFrame:
    """One row per (CCN, EIN) read from price-file names; a CCN can have several EINs and one
    EIN (a health system) often covers many CCNs."""
    f = cat[cat["ein"] != ""]
    per_ein = f.groupby("ein")["ccn"].nunique()
    rows = []
    for (ccn, ein), g in f.groupby(["ccn", "ein"], sort=True):
        h = g.iloc[0]
        npis = sorted({n for n in g["npi"] if n})
        rows.append({"ccn": ccn, "ein": ein, "name": h["name"], "city": h["city"], "state": h["state"],
                     "npi": " ".join(npis), "n_price_files": len(g),
                     "ccns_with_this_ein": int(per_ein.get(ein, 1)),
                     "example_price_file": g["mrf_url"].iloc[0]})
    for r in rows:
        r["source"] = "price-file name"
    # Fill hospitals with no EIN from their price files using IRS 990 Schedule H (nonprofits).
    fac = _read(ROOT / "data" / "registry" / "irs990_hospital_facilities.csv")
    if not fac.empty:
        hosp = cat.drop_duplicates("ccn")
        have = {r["ccn"] for r in rows}
        info = hosp.set_index("ccn")
        for m in ein_mod.match_990(hosp[hosp["hpt_exempt"] != "True"], fac):
            if m["ccn"] in have:
                continue
            h = info.loc[m["ccn"]]
            rows.append({"ccn": m["ccn"], "ein": m["ein"], "name": h["name"], "city": h["city"], "state": h["state"],
                         "npi": "", "n_price_files": 0, "ccns_with_this_ein": 0, "example_price_file": "",
                         "source": f"IRS 990 Schedule H ({m['irs_filer']}, tax year ending {m['irs_tax_period']}; "
                                   f"facility '{m['irs_facility']}', matched by {m['method']})"})
            have.add(m["ccn"])
    df = pd.DataFrame(rows, columns=["ccn", "ein", "name", "city", "state", "npi", "n_price_files",
                                     "ccns_with_this_ein", "example_price_file", "source"])
    if len(df):
        df["ccns_with_this_ein"] = df["ein"].map(df.groupby("ein")["ccn"].nunique())
    return df.sort_values(["state", "name", "ccn"]).reset_index(drop=True) if len(df) else df


def care_compare_profiles() -> dict[str, str]:
    """CCN -> Medicare Care Compare profile type (hospital, inpatient-rehabilitation,
    long-term-care-hospital), only for CCNs that have that profile. From data/cms/care_compare_ccns.csv
    (scripts/care_compare_ccns.py); hospital profiles fall back to Hospital General Information."""
    out: dict[str, str] = {}
    hgi = ROOT / "data" / "cms" / "hospital_general_information.csv"
    if hgi.exists():
        for c in _read(hgi).get("Facility ID", []):
            out[str(c).zfill(6)] = "hospital"
    cc = ROOT / "data" / "cms" / "care_compare_ccns.csv"
    if cc.exists():
        for r in _read(cc).itertuples():  # a rehab or LTCH profile is the more specific page
            if r.profile != "hospital" or r.ccn not in out:
                out[r.ccn] = r.profile
    return out


def exempt_eins() -> list[str]:
    """EINs of tax-exempt organizations (IRS EO Business Master File, via scripts/exempt_eins.py):
    the ones with a ProPublica Nonprofit Explorer page."""
    df = _read(ROOT / "data" / "registry" / "exempt_eins.csv")
    return sorted(df["ein"]) if len(df) else []


def build() -> Path:
    cat = catalog()
    xw = ein_crosswalk(cat)
    xw.to_csv(ROOT / "data" / "ccn_ein.csv", index=False)
    if len(xw):  # hospitals whose EIN comes from IRS 990 only: show it in the table too
        irs = xw[xw["source"].str.startswith("IRS")].set_index("ccn")["ein"]
        cat = cat.copy()
        fill = cat["ccn"].isin(irs.index) & (cat["ein"] == "")
        cat.loc[fill, "ein"] = cat.loc[fill, "ccn"].map(irs)
    cat = cat.copy()
    cat["care_compare"] = cat["ccn"].map(care_compare_profiles()).fillna("")
    cat.to_csv(ROOT / "data" / "catalog.csv", index=False)

    crawled = cat[cat["match_status"] != ""]
    lines = [
        "# Hospital Price Transparency Hub",
        "",
        f"{crawled['ccn'].nunique()} of {cat['ccn'].nunique()} CMS hospitals, each with its website, its `cms-hpt.txt` "
        f"and a direct link to every price file. Full table: [`data/catalog.csv`](data/catalog.csv). "
        "Sources: every active hospital in the CMS Provider of Services file Q3 2026, with bed counts "
        "(`BED_CNT`, per CCN), names from CMS [Hospital General Information]"
        "(https://data.cms.gov/provider-data/dataset/xubh-q36u) where listed, price-file links from each hospital's own `cms-hpt.txt`. "
        "See [README](README.md#sources).",
        "",
        "| # | Hospital | City, State | Beds (CCN) | cms-hpt.txt | Location name (from txt) | Price file | Format | Size (MB) | Last modified | Contact | Match |",
        "|---|---|---|---:|---|---|---|---|---:|---|---|---|",
    ]
    for i, r in enumerate(crawled.itertuples(index=False), 1):
        live = r.hpt_txt_url if r.hpt_txt_source in ("live", "") and str(r.hpt_txt_url).startswith("http") else ""
        txt = _md_link(live or f"https://{r.hpt_txt_domain}/cms-hpt.txt", r.hpt_txt_domain) if r.hpt_txt_domain else "not found"
        fname = r.mrf_url.rstrip("/").split("/")[-1].split("?")[0][:40] if r.mrf_url else ""
        contact = r.contact_name + (f" ({r.contact_email})" if r.contact_email else "")
        cells = [str(i), f"{r.name} (CCN {r.ccn})", f"{r.city}, {r.state}", r.beds, txt,
                 _md_link(r.source_page_url, r.location_name) or r.location_name,
                 _md_link(r.mrf_url, fname or "link"), r.mrf_format, r.mrf_size_mb, r.mrf_last_modified,
                 contact, r.match_status]
        lines.append("| " + " | ".join(str(c).replace("|", "\\|") for c in cells) + " |")
    (ROOT / "CATALOG.md").write_text("\n".join(lines) + "\n")

    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "totals": price_totals(cat),
        "columns": list(cat.columns),
        "rows": cat.values.tolist(),
        "exempt_eins": exempt_eins(),
    }
    out = ROOT / "data.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, separators=(",", ":")))
    return out
