"""EIN (and NPI) from price-file names.

CMS requires each machine-readable file to be named
``<EIN>_<hospital-name>_standardcharges.<ext>`` (45 CFR 180.50(d)(5)), often with the
hospital's NPI after the EIN (``<EIN>-<NPI>_...``). The EIN is read from the file name
in the price-file URL only; no file is ever downloaded.
"""

from __future__ import annotations

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
