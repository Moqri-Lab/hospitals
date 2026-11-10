"""Parse the CMS `cms-hpt.txt` discovery file.

Since 2025 every hospital must publish a plain-text file at the root of its
public website (``https://<domain>/cms-hpt.txt``) that lists one block per
hospital location::

    location-name: West Mercy Hospital
    source-page-url: https://westmercy.org/price-transparency
    mrf-url: https://westmercy.org/files/12-3456789_west-mercy_standardcharges.csv
    contact-name: Jane Doe
    contact-email: jane.doe@westmercy.org

Blocks are separated by blank lines. Real-world files are messy, so the parser
is lenient: keys are case-insensitive, separators may be ``:`` or ``=``,
blocks may be split by a repeated ``location-name`` instead of a blank line,
and BOMs / CRLF / stray HTML are tolerated or flagged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict

KNOWN_KEYS = ("location-name", "source-page-url", "mrf-url", "contact-name", "contact-email")
REQUIRED_KEYS = ("location-name", "source-page-url", "mrf-url", "contact-name", "contact-email")

_KEY_ALIASES = {
    "location_name": "location-name",
    "locationname": "location-name",
    "location": "location-name",
    "source_page_url": "source-page-url",
    "sourcepageurl": "source-page-url",
    "source-page": "source-page-url",
    "mrf_url": "mrf-url",
    "mrfurl": "mrf-url",
    "mrf": "mrf-url",
    "contact_name": "contact-name",
    "contactname": "contact-name",
    "contact_email": "contact-email",
    "contactemail": "contact-email",
}

_LINE_RE = re.compile(r"^\s*([A-Za-z_\- ]{2,40}?)\s*[:=]\s*(.*?)\s*$")


@dataclass
class HptEntry:
    location_name: str = ""
    source_page_url: str = ""
    mrf_url: str = ""
    contact_name: str = ""
    contact_email: str = ""
    issues: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["issues"] = "; ".join(self.issues)
        return d


@dataclass
class HptFile:
    entries: list[HptEntry]
    issues: list[str]

    @property
    def ok(self) -> bool:
        return bool(self.entries) and not any(i.startswith("fatal") for i in self.issues)


def _norm_key(raw: str) -> str | None:
    k = raw.strip().lower().replace(" ", "-")
    if k in KNOWN_KEYS:
        return k
    return _KEY_ALIASES.get(k) or _KEY_ALIASES.get(k.replace("-", "_"))


def parse(text: str) -> HptFile:
    issues: list[str] = []
    if text.startswith("﻿"):
        text = text[1:]
    head = text[:2000].lower()
    if "<html" in head or "<!doctype" in head:
        return HptFile([], ["fatal: response is HTML, not a cms-hpt.txt file"])

    entries: list[HptEntry] = []
    cur: dict[str, str] = {}

    def flush():
        if cur:
            e = HptEntry(
                location_name=cur.get("location-name", ""),
                source_page_url=cur.get("source-page-url", ""),
                mrf_url=cur.get("mrf-url", ""),
                contact_name=cur.get("contact-name", ""),
                contact_email=cur.get("contact-email", ""),
            )
            for k in REQUIRED_KEYS:
                if not cur.get(k):
                    e.issues.append(f"missing {k}")
            if e.mrf_url and not e.mrf_url.lower().startswith(("http://", "https://")):
                e.issues.append("mrf-url is not an absolute http(s) URL")
            entries.append(e)
        cur.clear()

    unknown = set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            if not line:
                flush()
            continue
        m = _LINE_RE.match(line)
        if not m:
            continue
        key = _norm_key(m.group(1))
        if key is None:
            unknown.add(m.group(1).strip().lower())
            continue
        # a repeated key without a blank line starts a new block
        if key in cur:
            flush()
        cur[key] = m.group(2)
    flush()

    if unknown:
        issues.append("unknown keys: " + ", ".join(sorted(unknown))[:200])
    if not entries:
        issues.append("fatal: no location blocks found")
    return HptFile(entries, issues)
