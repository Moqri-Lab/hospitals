"""EIN (and NPI) from price-file names.

CMS requires each machine-readable file to be named
``<EIN>_<hospital-name>_standardcharges.<ext>`` (45 CFR 180.50(d)(5)), often with the
hospital's NPI after the EIN (``<EIN>-<NPI>_...``). The EIN is read from the file name
in the price-file URL only; no file is ever downloaded.
"""

from __future__ import annotations

import html
import re
import urllib.parse as up

_EIN = re.compile(r"^(\d{2})-?(\d{7})(?!\d)(?:[-_](\d{10})(?!\d))?[-_]")
_NPI = re.compile(r"^([12]\d{9})(?!\d)[-_]")
# EIN prefixes the IRS has never assigned.
_BAD_PREFIX = {"00", "07", "08", "09", "17", "18", "19", "28", "29", "49", "69", "70", "78", "79", "89", "96", "97"}


def _names(url: str) -> list[str]:
    p = up.urlparse(str(url))
    path = up.unquote(p.path)
    out = [re.split(r"[\\/]", path)[-1]]
    out += [re.split(r"[\\/]", v)[-1] for v in re.split(r"[&=]", up.unquote(p.query)) if v]
    return [n for n in out if n]


def from_url(url: str) -> tuple[str, str]:
    """(ein, npi) parsed from a price-file URL; empty strings when the name carries none."""
    names = _names(url)
    ranked = sorted(names, key=lambda n: "standardcharges" not in re.sub(r"[-_ ]", "", n.lower()))
    for n in ranked:
        m = _EIN.match(n)
        if m and m.group(1) not in _BAD_PREFIX:
            return f"{m.group(1)}-{m.group(2)}", m.group(3) or ""
    for n in ranked:
        m = _NPI.match(n)
        if m:
            return "", m.group(1)
    return "", ""



def from_parts(url: str, filename: str = "", final_url: str = "") -> tuple[str, str]:
    """(ein, npi) from the link, else the server-given file name, else the link after redirects."""
    best = ("", "")
    for cand in (url, f"x:///{filename}" if filename else "", final_url):
        if not cand:
            continue
        e, n = from_url(cand)
        if e:
            return e, n or best[1]
        if n and not best[1]:
            best = ("", n)
    return best

# ---------------------------------------------------------------- IRS 990 Schedule H

_STOPW = {"THE", "OF", "AND", "INC", "LLC", "AT", "A", "HOSPITAL", "MEDICAL", "CENTER", "HEALTH", "CAMPUS",
          "REGIONAL", "COMMUNITY", "MEMORIAL", "SYSTEM", "HEALTHCARE", "CORP", "CORPORATION", "DBA"}
_ABBR = {"ST": "SAINT", "STE": "SAINTE", "MT": "MOUNT", "HOSP": "HOSPITAL", "CTR": "CENTER", "MED": "MEDICAL",
         "MEM": "MEMORIAL", "REG": "REGIONAL", "HLTH": "HEALTH", "UNIV": "UNIVERSITY", "CO": "COUNTY"}


def _tok(s: str) -> set[str]:
    w = re.sub(r"[^A-Z0-9 ]", " ", str(s).upper().replace("'", "").replace("&", " AND ")).split()
    return {_ABBR.get(x, x) for x in w} - _STOPW


def _street_no(a: str) -> str:
    m = re.match(r"\s*(\d+)", str(a))
    return m.group(1) if m else ""


def match_990(reg, fac) -> list[dict]:
    """Match Schedule H facilities to CMS hospitals in the same state.

    A facility matches a hospital when the ZIP code agrees and either the street number agrees
    or the distinctive name words overlap (Jaccard >= 0.5); or, across the state, when the
    distinctive name words are identical and the city agrees. Ambiguous facilities are skipped.
    """
    by_state: dict[str, list] = {}
    # Schedule H is filed by nonprofit (and some government) hospitals only; skip for-profit,
    # physician-owned and federal ones, which can share a building with a nonprofit hospital.
    reg = reg[~reg["ownership"].str.contains("Proprietary|Physician|Federal|Defense|Veterans", regex=True)]
    for h in reg.itertuples(index=False):
        by_state.setdefault(h.state, []).append((h, _tok(h.name), str(h.zip)[:5], _street_no(h.address)))
    out = []
    for f in fac.itertuples(index=False):
        ft = _tok(html.unescape(f"{f.facility_name} {f.facility_name2}"))
        fz, fn = str(f.zip)[:5], _street_no(f.address)
        hits = []
        for h, ht, hz, hn in by_state.get(f.state, []):
            jac = len(ft & ht) / len(ft | ht) if ft and ht else 0.0
            # Same street number: the names must also agree (one set of words inside the other),
            # since system returns often give every facility the headquarters address.
            if hz == fz and ((fn and fn == hn and ft and ht and (ft <= ht or ht <= ft)) or jac >= 0.5):
                hits.append((h, "zip+" + ("address" if fn and fn == hn else "name")))
            elif ft and ft == ht and str(h.city).upper() == str(f.city).upper():
                hits.append((h, "name+city"))
        if len({h.ccn for h, _ in hits}) == 1:
            h, how = hits[0]
            out.append({"ccn": h.ccn, "ein": f.ein, "irs_filer": f.filer_name, "irs_facility": f.facility_name,
                        "irs_tax_period": f.tax_period_end, "method": how})
    return out
