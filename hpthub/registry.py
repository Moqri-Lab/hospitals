"""Build the hospital registry.

The registry is one row per CMS Certification Number (CCN) with the hospital's
name, location, type, bed count and the website domain(s) where its
``cms-hpt.txt`` should live.

Sources (official CMS only):

1. CMS Hospital General Information (data.cms.gov dataset ``xubh-q36u``):
   the list of Medicare-certified hospitals with CCN, type and ownership.
2. CMS Provider of Services (POS) hospital file, latest quarter: joined on
   CCN for bed counts, active providers only (PGM_TRMNTN_CD == "00").

No official source lists hospital websites, so the domain where each
hospital's ``cms-hpt.txt`` lives is kept in ``data/registry/domains.csv``.
Every domain there is verified by fetching its ``cms-hpt.txt`` and matching the
listed location names back to the CMS hospital.
"""

from __future__ import annotations

import csv
import io
import re
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd

from . import http

ROOT = Path(__file__).resolve().parent.parent
REG_DIR = ROOT / "data" / "registry"

HGI_METASTORE = "https://data.cms.gov/provider-data/api/1/metastore/schemas/dataset/items/xubh-q36u"
CMS_CATALOG = "https://data.cms.gov/data.json"
# The POS hospital file is titled "... Quality Improvement and Evaluation System : <quarter>"
# in the data.json catalog (files are named Hospital_and_other.DATA.Q<n>_<year>.csv).
POS_TITLES = ("Provider of Services File - Quality Improvement and Evaluation System",
              "Provider of Services File - Hospital & Non-Hospital Facilities")
CMS_DIR = ROOT / "data" / "cms"

# Hosting/CDN/vendor hosts that are not a hospital's own website, so they can't
# hold the hospital's cms-hpt.txt.
VENDOR_HOST_PATTERNS = [
    r"blob\.core\.windows\.net$", r"amazonaws\.com$", r"cloudfront\.net$", r"googleapis\.com$",
    r"azureedge\.net$", r"sharepoint\.com$", r"dropbox", r"box\.com$", r"github",
    r"hospitalpricedisclosure\.com$", r"panaceainc\.com$", r"pricetransparency", r"cdmpricing",
    r"hospitalprices", r"apps\.para-hcfs\.com$", r"claraprice", r"turquoise", r"healthsparq",
    r"mrf\.", r"standardcharges", r"chargemaster", r"craneware", r"pricelist", r"sgprt",
    r"carefinance\.", r"cms-hpt", r"patientrightsadvocate",
]
_VENDOR_RE = re.compile("|".join(VENDOR_HOST_PATTERNS), re.I)

# Federal (VA / DoD) hospitals are deemed compliant under 45 CFR 180.30(b) and are excluded.
ACUTE_TYPES = {"short term", "acute care hospitals", "childrens hospitals", "childrens",
               "critical access hospitals", "transplant hospitals"}


def host_of(url: str) -> str:
    if not isinstance(url, str) or not url.strip():
        return ""
    u = url.strip()
    if "://" not in u:
        u = "https://" + u
    try:
        h = (urlparse(u).hostname or "").lower()
    except ValueError:
        return ""
    return h


def base_domain(host: str) -> str:
    """Crude eTLD+1 that is right for nearly all US hospital domains."""
    parts = [p for p in host.split(".") if p]
    if len(parts) <= 2:
        return ".".join(parts)
    # keep three labels for things like foo.k12.ca.us / foo.state.us / x.co.uk
    if parts[-1] == "us" or (len(parts[-2]) <= 3 and parts[-2] in {"co", "com", "org", "net", "gov", "edu", "ac"}):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def is_vendor_host(host: str) -> bool:
    return bool(host) and bool(_VENDOR_RE.search(host))


def candidate_hosts(*urls: str) -> list[str]:
    """Return candidate website hosts (most specific first) for a set of URLs."""
    out: list[str] = []
    for u in urls:
        h = host_of(u)
        if not h or is_vendor_host(h):
            continue
        b = base_domain(h)
        for cand in (h, "www." + b, b):
            if cand and cand not in out:
                out.append(cand)
    return out


# ---------------------------------------------------------------- live CMS

def _download_csv(sess, url: str) -> pd.DataFrame:
    r = sess.get(url, timeout=300)
    r.raise_for_status()
    return pd.read_csv(io.BytesIO(r.content), dtype=str, keep_default_na=False, low_memory=False)


def download_cms(out_dir: Path = CMS_DIR, sess=None) -> None:
    """Download Hospital General Information + the latest POS hospital file to ``out_dir``."""
    sess = sess or http.session()
    out_dir.mkdir(parents=True, exist_ok=True)
    meta = sess.get(HGI_METASTORE, timeout=60).json()
    dl = next(d["downloadURL"] for d in meta["distribution"] if d.get("downloadURL", "").endswith(".csv"))
    _download_csv(sess, dl).to_csv(out_dir / "hospital_general_information.csv", index=False)

    cat = sess.get(CMS_CATALOG, timeout=180).json()
    dists = []
    for ds in cat["dataset"]:
        title = ds.get("title", "").strip()
        if title.startswith(POS_TITLES):
            for d in ds.get("distribution", []):
                url = str(d.get("downloadURL", ""))
                if url.endswith(".csv"):
                    dists.append((d.get("temporal", "") or d.get("modified", "") or title, url, title))
    dists.sort(reverse=True)
    _, url, title = dists[0]
    pos = _download_csv(sess, url)
    pos.columns = [c.upper() for c in pos.columns]
    if "PRVDR_CTGRY_CD" in pos.columns:
        pos = pos[pos["PRVDR_CTGRY_CD"] == "01"]
    keep = [c for c in ("PRVDR_NUM", "FAC_NAME", "ST_ADR", "CITY_NAME", "STATE_CD", "ZIP_CD",
                        "PRVDR_CTGRY_SBTYP_CD", "BED_CNT", "CRTFD_BED_CNT", "GNRL_CNTL_TYPE_CD",
                        "PGM_TRMNTN_CD") if c in pos.columns]
    pos[keep].to_csv(out_dir / "pos_hospitals.csv", index=False)
    (out_dir / "pos_source.txt").write_text(f"Source: {title}\nURL: {url}\nFilter: PRVDR_CTGRY_CD == \"01\"\n")


def load_cms(cms_dir: Path = CMS_DIR) -> pd.DataFrame:
    """Join CMS Hospital General Information to POS bed counts (active providers) on CCN."""
    hgi = pd.read_csv(cms_dir / "hospital_general_information.csv", dtype=str, keep_default_na=False)
    hgi = hgi.rename(columns={
        "Facility ID": "ccn", "Facility Name": "name", "Address": "address", "City/Town": "city",
        "State": "state", "ZIP Code": "zip", "Hospital Type": "facility_type",
        "Hospital Ownership": "ownership",
    })
    hgi["ccn"] = hgi["ccn"].str.strip().str.zfill(6)
    pos = pd.read_csv(cms_dir / "pos_hospitals.csv", dtype=str, keep_default_na=False)
    if "PGM_TRMNTN_CD" in pos.columns:
        pos = pos[pos["PGM_TRMNTN_CD"] == "00"]
    beds = pos[["PRVDR_NUM", "BED_CNT"]].rename(columns={"PRVDR_NUM": "ccn", "BED_CNT": "beds"})
    beds["ccn"] = beds["ccn"].str.strip().str.zfill(6)
    df = hgi.merge(beds.drop_duplicates("ccn"), on="ccn", how="left")
    for c in ("name", "address", "city"):
        df[c] = df[c].str.title()
    df["source"] = "cms-hgi+pos"
    return df


# ---------------------------------------------------------------- build

REGISTRY_COLUMNS = [
    "ccn", "name", "address", "city", "state", "zip", "facility_type", "ownership", "beds",
    "hpt_exempt", "system", "domains", "domain_source",
    "web_manual", "web_system_rule", "web_wikidata", "wikidata_id", "web_tpafs", "web_hpf", "source",
]

LEAD_SOURCE_NAMES = {
    "wikidata": "Wikidata",
    "tpafs": "TPAFS/DoltHub transparency-data",
    "sutton": "hospitalpricingfiles.org (via nathansutton/hospital-price-transparency)",
}


def load_lead_rows() -> dict[str, list[tuple[str, list[str]]]]:
    """ccn -> [(source, [domains])] for every lead source, kept separate."""
    p = REG_DIR / "domain_leads.csv"
    out: dict[str, list[tuple[str, list[str]]]] = {}
    if p.exists():
        with p.open(newline="") as f:
            for r in csv.DictReader(f):
                if r.get("domains"):
                    out.setdefault(r["ccn"].zfill(6), []).append((r["source"], r["domains"].split()))
    return out


def load_wikidata_ids() -> dict[str, str]:
    p = REG_DIR / "wikidata_hospitals.csv"
    if not p.exists():
        return {}
    with p.open(newline="") as f:
        return {r["ccn"].zfill(6): r["wikidata_id"] for r in csv.DictReader(f) if r.get("wikidata_id")}


def build(cms_dir: Path = CMS_DIR, min_beds: int = 0, acute_only: bool = False) -> pd.DataFrame:
    """Registry = every CMS Hospital General Information hospital, with POS bed counts.

    Sorted by bed count (largest first) so ``--limit N`` crawls the N largest.
    """
    df = load_cms(cms_dir)
    for col in REGISTRY_COLUMNS:
        if col not in df.columns:
            df[col] = ""
    df["beds"] = pd.to_numeric(df["beds"], errors="coerce")
    ft = df["facility_type"].str.lower()
    # Federal (VA / DoD) hospitals are deemed compliant under 45 CFR 180.30(b).
    df["hpt_exempt"] = ft.str.contains("veterans|defense", regex=True)
    if acute_only:
        df = df[(ft.str.contains("acute|children", regex=True)) & ~df["hpt_exempt"]]
    if min_beds:
        df = df[df["beds"] >= min_beds]
    df = df.copy()
    overrides = load_overrides()
    rules = load_system_rules()
    leads = load_leads()

    def domains(row) -> tuple[str, str, str]:
        if row["ccn"] in overrides:
            return overrides[row["ccn"]], "", "override"
        key = f"{row['name']} {row['state']}"
        for rx, doms, system in rules:
            if rx.search(key):
                lead = leads.get(row["ccn"], ("", ""))[0].split()
                extra = [d for d in lead if d not in doms.split()]
                return " ".join(doms.split() + extra), system, "system-rule" + ("+lead" if extra else "")
        if row["ccn"] in leads:
            return leads[row["ccn"]][0], "", "lead:" + leads[row["ccn"]][1]
        return "", "", ""

    res = df.apply(domains, axis=1, result_type="expand")
    df["domains"], df["system"], df["domain_source"] = res[0], res[1], res[2]

    # One column per website source, keyed by CCN: the domain(s) each source gives.
    lead_rows, wd = load_lead_rows(), load_wikidata_ids()

    def per_source(row) -> list[str]:
        ccn = row["ccn"]
        key = f"{row['name']} {row['state']}"
        rule = next((doms for rx, doms, _ in rules if rx.search(key)), "")
        leads_by = {src: " ".join(d) for src, d in lead_rows.get(ccn, [])}
        return [overrides.get(ccn, ""), rule, leads_by.get("wikidata", ""), wd.get(ccn, ""),
                leads_by.get("tpafs", ""), leads_by.get("sutton", "")]

    cols = ["web_manual", "web_system_rule", "web_wikidata", "wikidata_id", "web_tpafs", "web_hpf"]
    df[cols] = df.apply(per_source, axis=1, result_type="expand")
    df["beds"] = df["beds"].astype("Int64")
    df = df[REGISTRY_COLUMNS].sort_values(["beds", "ccn"], ascending=[False, True], na_position="last")
    return df.reset_index(drop=True)


def load_leads() -> dict[str, tuple[str, str]]:
    """Candidate domains from existing open datasets (see scripts/import_domain_leads.py)."""
    p = REG_DIR / "domain_leads.csv"
    if not p.exists():
        return {}
    out: dict[str, tuple[str, str]] = {}
    with p.open(newline="") as f:
        for r in csv.DictReader(f):
            if not r.get("domains"):
                continue
            ccn = r["ccn"].zfill(6)
            doms, srcs = out.get(ccn, ("", ""))
            merged = doms.split() + [d for d in r["domains"].split() if d not in doms.split()]
            out[ccn] = (" ".join(merged), "+".join(x for x in (srcs, r["source"]) if x))
    return out


def load_system_rules() -> list[tuple[re.Pattern, str, str]]:
    """Ordered (regex, domains, system) rules matched against "<name> <STATE>"."""
    p = REG_DIR / "system_domains.csv"
    if not p.exists():
        return []
    out = []
    with p.open(newline="") as f:
        for r in csv.DictReader(f):
            flags = 0 if re.search(r"\\b[A-Z]{2}\\b", r["pattern"]) else re.I
            out.append((re.compile(r["pattern"], re.I), r["domains"].strip(), r["system"].strip()))
    return out


def load_overrides() -> dict[str, str]:
    p = REG_DIR / "domains.csv"
    if not p.exists():
        return {}
    with p.open(newline="") as f:
        return {r["ccn"].zfill(6): r["domains"].strip() for r in csv.DictReader(f) if r.get("domains")}


def save(df: pd.DataFrame, path: Path | None = None) -> Path:
    path = path or REG_DIR / "hospitals.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)
    return path


def load(path: Path | None = None) -> pd.DataFrame:
    path = path or REG_DIR / "hospitals.csv"
    return pd.read_csv(path, dtype=str, keep_default_na=False)
