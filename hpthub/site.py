"""Build the catalog: one row per (hospital, price file).

Outputs
- ``data/catalog.csv``: full catalog (every registry hospital; crawled ones have
  their cms-hpt.txt fields and price-file link, format, size and date)
- ``CATALOG.md``: the crawled hospitals as a Markdown table, readable on GitHub
- ``site/data.json``: data for the browsable table in ``site/index.html``
"""

from __future__ import annotations

import datetime as dt
import json
from email.utils import parsedate_to_datetime
from pathlib import Path

import pandas as pd

from . import registry
from .discover import plausible_size

ROOT = registry.ROOT

CATALOG_COLUMNS = [
    "ccn", "name", "address", "city", "state", "zip", "facility_type", "ownership", "beds", "n_campuses", "system",
    "hpt_exempt", "exempt_note", "web_claude", "web_system_rule", "web_wikidata", "wikidata_id", "web_tpafs", "web_hpf", "hpt_txt_domain", "hpt_txt_found", "hpt_txt_source", "hpt_txt_access_issue", "location_name", "source_page_url", "mrf_url",
    "hpt_txt_url", "mrf_format", "mrf_size_mb", "mrf_last_modified", "mrf_http_status", "contact_name",
    "contact_email", "match_status", "match_score", "checked_at",
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
        reg["hpt_txt_url"] = reg["hpt_txt_domain"].map(d["url"]).fillna("")
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
    reg["checked_at"] = dt.date.today().isoformat()
    reg.loc[reg["match_status"] == "", "checked_at"] = ""
    return reg[CATALOG_COLUMNS]


def _md_link(url: str, text: str) -> str:
    return f"[{text}]({url})" if url else ""


def build() -> Path:
    cat = catalog()
    cat.to_csv(ROOT / "data" / "catalog.csv", index=False)

    crawled = cat[cat["match_status"] != ""]
    lines = [
        "# Hospital price file catalog",
        "",
        f"{crawled['ccn'].nunique()} of {cat['ccn'].nunique()} CMS hospitals crawled so far "
        f"(largest first). Full table: [`data/catalog.csv`](data/catalog.csv). "
        "Price files are listed, never downloaded. Sources: CMS [Hospital General Information]"
        "(https://data.cms.gov/provider-data/dataset/xubh-q36u), bed counts (`BED_CNT`, per CCN) from the CMS "
        "Provider of Services file Q3 2026, price-file links from each hospital's own `cms-hpt.txt`. "
        "See [README](README.md#sources).",
        "",
        "| # | Hospital | City, State | Beds (CCN) | cms-hpt.txt | Location name (from txt) | Price file | Format | Size (MB) | Last modified | Contact | Match |",
        "|---|---|---|---:|---|---|---|---|---:|---|---|---|",
    ]
    for i, r in enumerate(crawled.itertuples(index=False), 1):
        txt = _md_link(f"https://{r.hpt_txt_domain}/cms-hpt.txt", r.hpt_txt_domain) if r.hpt_txt_domain else "not found"
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
        "columns": CATALOG_COLUMNS,
        "rows": cat.values.tolist(),
    }
    out = ROOT / "site" / "data.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, separators=(",", ":")))
    return out
