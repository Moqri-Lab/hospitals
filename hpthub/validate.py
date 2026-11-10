"""Lightweight validator for hospital machine-readable files (MRFs).

It checks a file against the CMS Hospital Price Transparency templates
(v2.0-v2.2 and v3.0, CSV tall / CSV wide / JSON) by reading only the head of
a *local* file, so even multi-GB files can be screened quickly. By project
policy the hub never downloads price data; this module only works on files a
researcher already has on disk. It is not a
replacement for the official CMS validator, but it catches the issues that
matter for research use: wrong format, missing required headers, invalid
enumerations, non-numeric prices and stale or missing dates.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path

from . import http

SETTINGS = {"inpatient", "outpatient", "both"}
CODE_TYPES = {"cpt", "hcpcs", "icd", "drg", "ms-drg", "r-drg", "s-drg", "aps-drg", "ap-drg", "apr-drg",
              "tris-drg", "apc", "ndc", "hipps", "local", "eapg", "cdt", "rc", "cdm", "cmg", "ms-ltc-drg"}
METHODOLOGIES = {"case rate", "fee schedule", "percent of total billed charges", "per diem", "other"}
DRUG_TYPES = {"gr", "ml", "me", "un", "f2", "gm", "ea"}

ROW1_COMMON = ["hospital_name", "last_updated_on", "version", "hospital_address"]
ROW1_V2 = ["hospital_location"]
ROW1_V3 = ["location_name", "type_2_npi", "attester_name"]
ROW3_COMMON = ["description", "setting", "standard_charge|gross", "standard_charge|discounted_cash",
               "standard_charge|min", "standard_charge|max", "additional_generic_notes"]
ROW3_TALL = ["payer_name", "plan_name", "standard_charge|negotiated_dollar",
             "standard_charge|negotiated_percentage", "standard_charge|negotiated_algorithm",
             "standard_charge|methodology"]
ROW3_TALL_V2 = ["estimated_amount"]
ROW3_TALL_V3 = ["median_amount", "10th_percentile", "90th_percentile", "count"]

NUMERIC_TALL = ["standard_charge|gross", "standard_charge|discounted_cash", "standard_charge|min",
                "standard_charge|max", "standard_charge|negotiated_dollar",
                "standard_charge|negotiated_percentage", "estimated_amount", "median_amount",
                "10th_percentile", "90th_percentile"]

FILENAME_RE = re.compile(r"^\d{2}-?\d{7}_.+_standardcharges\.(csv|json|zip)$", re.I)
COUNT_RE = re.compile(r"^(0|1 through 10|[1-9]\d*)$", re.I)


def norm_header(h: str) -> str:
    """Canonical form of a CSV header: lower-case, no spaces around pipes."""
    return re.sub(r"\s*\|\s*", "|", (h or "").strip().lower())


@dataclass
class Report:
    source: str
    format: str = ""            # csv-tall | csv-wide | json | zip | html | unknown
    version: str = ""
    hospital_name: str = ""
    last_updated_on: str = ""
    location_names: str = ""
    license_state: str = ""
    n_payer_plans: int | None = None
    rows_sampled: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    http_status: int | None = None
    content_length: int | None = None
    last_modified: str = ""
    filename_ok: bool | None = None

    @property
    def status(self) -> str:
        if self.format in ("", "unknown", "html") or self.http_status not in (None, 200):
            return "unreadable"
        return "valid" if not self.errors else "invalid"

    def as_row(self) -> dict:
        d = asdict(self)
        d["status"] = self.status
        d["n_errors"] = len(self.errors)
        d["n_warnings"] = len(self.warnings)
        d["errors"] = " | ".join(self.errors[:20])
        d["warnings"] = " | ".join(self.warnings[:20])
        return d


def _is_num(v: str) -> bool:
    try:
        float(v.replace(",", ""))
        return True
    except ValueError:
        return False


def _parse_date(v: str) -> dt.date | None:
    v = (v or "").strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%-m/%-d/%Y"):
        try:
            return dt.datetime.strptime(v, fmt).date()
        except ValueError:
            continue
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", v)
    if m:
        try:
            return dt.date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None
    return None


def _check_date(rep: Report, v: str, today: dt.date | None = None) -> None:
    d = _parse_date(v)
    if d is None:
        rep.errors.append(f"last_updated_on '{v[:30]}' is not a valid date")
        return
    today = today or dt.date.today()
    age = (today - d).days
    if age > 365:
        rep.warnings.append(f"file is stale: last_updated_on {d} is {age} days old")
    if age < -1:
        rep.warnings.append(f"last_updated_on {d} is in the future")


# ------------------------------------------------------------------ CSV

def validate_csv_text(text: str, rep: Report, max_rows: int = 2000, truncated: bool = False) -> Report:
    # drop a possibly partial last line when we only have the head of the file
    if truncated and "\n" in text:
        text = text[: text.rfind("\n")]
    reader = csv.reader(io.StringIO(text))
    try:
        row1 = next(reader)
        row2 = next(reader)
        row3 = next(reader)
    except StopIteration:
        rep.format = "unknown"
        rep.errors.append("fewer than 3 rows: not a CMS template CSV")
        return rep
    except csv.Error as e:
        rep.format = "unknown"
        rep.errors.append(f"CSV parse error in header rows: {e}")
        return rep

    h1 = [norm_header(h) for h in row1]
    h3 = [norm_header(h) for h in row3]
    if "hospital_name" not in h1 and "description" not in h3:
        rep.format = "unknown"
        rep.errors.append("not a CMS template CSV (no hospital_name in row 1 / description in row 3)")
        return rep

    meta = {h: (row2[i].strip() if i < len(row2) else "") for i, h in enumerate(h1) if h}
    rep.version = meta.get("version", "")
    rep.hospital_name = meta.get("hospital_name", "")
    rep.last_updated_on = meta.get("last_updated_on", "")
    rep.location_names = meta.get("location_name", meta.get("hospital_location", ""))
    lic = [h for h in h1 if h.startswith("license_number")]
    major = rep.version.split(".")[0] if rep.version else ""

    is_tall = "payer_name" in h3
    payer_cols = [h for h in h3 if h.startswith("standard_charge|") and h.count("|") >= 3]
    rep.format = "csv-tall" if is_tall else "csv-wide"

    # ---- row 1
    for k in ROW1_COMMON:
        if k not in h1:
            rep.errors.append(f"row 1 missing '{k}'")
    if major == "3":
        for k in ROW1_V3:
            if k not in h1:
                rep.errors.append(f"row 1 missing '{k}' (required in v3)")
    elif major == "2":
        for k in ROW1_V2:
            if k not in h1:
                rep.errors.append(f"row 1 missing '{k}' (required in v2)")
    else:
        rep.errors.append(f"unrecognized template version '{rep.version}'")
    if not lic:
        rep.errors.append("row 1 missing 'license_number|[state]'")
    else:
        rep.license_state = lic[0].split("|")[-1].upper() if "|" in lic[0] else ""
        if not re.fullmatch(r"[A-Z]{2}", rep.license_state):
            rep.errors.append(f"license header '{lic[0]}' lacks a 2-letter state")
    attest = [h for h in h1 if h.startswith("to the best of its knowledge")]
    if not attest:
        rep.errors.append("row 1 missing the CMS attestation/affirmation statement header")
    else:
        val = meta.get(attest[0], "").lower()
        if val not in ("true", "false"):
            rep.errors.append("attestation value must be true or false")
        elif val == "false":
            rep.warnings.append("attestation is false")
    for k in ("hospital_name", "last_updated_on", "version"):
        if k in h1 and not meta.get(k):
            rep.errors.append(f"row 2 '{k}' is blank")
    if rep.last_updated_on:
        _check_date(rep, rep.last_updated_on)

    # ---- row 3
    for k in ROW3_COMMON:
        if k not in h3:
            rep.errors.append(f"row 3 missing '{k}'")
    if not any(re.fullmatch(r"code\|\d+", h) for h in h3):
        rep.errors.append("row 3 missing 'code|1'")
    if is_tall:
        for k in ROW3_TALL + (ROW3_TALL_V3 if major == "3" else ROW3_TALL_V2 if major == "2" else []):
            if k not in h3:
                rep.errors.append(f"row 3 missing '{k}'")
    else:
        plans = {tuple(h.split("|")[1:3]) for h in payer_cols}
        rep.n_payer_plans = len(plans)
        if not plans:
            rep.warnings.append("wide file has no payer-specific columns")
    if len(set(h3)) != len([h for h in h3 if h]):
        rep.errors.append("row 3 has duplicate headers")

    # ---- sample rows
    idx = {h: i for i, h in enumerate(h3)}
    code_type_cols = [i for h, i in idx.items() if re.fullmatch(r"code\|\d+\|type", h)]
    numeric_cols = [idx[h] for h in NUMERIC_TALL if h in idx]
    numeric_cols += [i for h, i in idx.items() if h.startswith(("standard_charge|", "median_amount|", "10th_percentile|", "90th_percentile|"))
                     and h.endswith(("negotiated_dollar", "negotiated_percentage")) or
                     (h.split("|")[0] in ("median_amount", "10th_percentile", "90th_percentile") and "|" in h)]
    meth_cols = [i for h, i in idx.items() if h.endswith("methodology")]
    count_cols = [i for h, i in idx.items() if h == "count" or h.startswith("count|")]
    bad = {"setting": 0, "code_type": 0, "numeric": 0, "methodology": 0, "count": 0, "description": 0}
    examples: dict[str, str] = {}
    payers = set()
    n = 0
    try:
        for row in reader:
            if not any(c.strip() for c in row):
                continue
            n += 1
            if n > max_rows:
                break
            g = lambda i: row[i].strip() if i is not None and i < len(row) else ""  # noqa: E731
            if "description" in idx and not g(idx["description"]):
                bad["description"] += 1
            if "setting" in idx:
                v = g(idx["setting"]).lower()
                if v not in SETTINGS:
                    bad["setting"] += 1; examples.setdefault("setting", v)
            for i in code_type_cols:
                v = g(i).lower()
                if v and v not in CODE_TYPES:
                    bad["code_type"] += 1; examples.setdefault("code_type", v)
            for i in numeric_cols:
                v = g(i)
                if v and (not _is_num(v) or v.startswith("$")):
                    bad["numeric"] += 1; examples.setdefault("numeric", f"{h3[i]}={v[:30]}")
            for i in meth_cols:
                v = g(i).lower()
                if v and v not in METHODOLOGIES:
                    bad["methodology"] += 1; examples.setdefault("methodology", v)
            for i in count_cols:
                v = g(i)
                if v and not COUNT_RE.match(v):
                    bad["count"] += 1; examples.setdefault("count", v)
            if is_tall and "payer_name" in idx and g(idx["payer_name"]):
                payers.add((g(idx["payer_name"]).lower(), g(idx.get("plan_name")).lower()))
    except csv.Error as e:
        rep.errors.append(f"CSV parse error after {n} data rows: {e}")
    rep.rows_sampled = min(n, max_rows)
    if is_tall:
        rep.n_payer_plans = len(payers)
    for k, c in bad.items():
        if c:
            ex = f" (e.g. '{examples[k]}')" if k in examples else ""
            rep.errors.append(f"{c} invalid {k} value(s) in first {rep.rows_sampled} rows{ex}")
    if rep.rows_sampled == 0:
        rep.errors.append("no data rows after headers")
    return rep


# ------------------------------------------------------------------ JSON

_JSON_KEY_RE = r'"{key}"\s*:\s*"([^"]*)"'


def validate_json_text(text: str, rep: Report, complete: bool, schema_dir: Path | None = None) -> Report:
    rep.format = "json"
    for key in ("hospital_name", "last_updated_on", "version"):
        m = re.search(_JSON_KEY_RE.format(key=key), text[:200_000])
        if m:
            setattr(rep, key, m.group(1))
    if not complete:
        # only the head of a large file: check top-level keys we can see
        for key in ("hospital_name", "last_updated_on", "version"):
            if not getattr(rep, key):
                rep.warnings.append(f"'{key}' not found in the first part of the file")
        if '"standard_charge_information"' not in text:
            rep.warnings.append("standard_charge_information not seen in sampled head")
        rep.warnings.append("large JSON: only header fields checked")
        if rep.last_updated_on:
            _check_date(rep, rep.last_updated_on)
        return rep
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as e:
        rep.errors.append(f"invalid JSON: {e}")
        return rep
    rep.hospital_name = str(doc.get("hospital_name", ""))
    rep.version = str(doc.get("version", ""))
    rep.last_updated_on = str(doc.get("last_updated_on", ""))
    locs = doc.get("location_name") or doc.get("hospital_location") or []
    rep.location_names = "|".join(locs) if isinstance(locs, list) else str(locs)
    rep.license_state = str((doc.get("license_information") or {}).get("state", ""))
    sci = doc.get("standard_charge_information") or []
    rep.rows_sampled = len(sci)
    rep.n_payer_plans = len({(p.get("payer_name"), p.get("plan_name"))
                             for item in sci for sc in item.get("standard_charges", [])
                             for p in sc.get("payers_information", []) or []})
    if rep.last_updated_on:
        _check_date(rep, rep.last_updated_on)
    schema = _schema_for(rep.version, schema_dir)
    if schema is None:
        rep.errors.append(f"unrecognized template version '{rep.version}'")
        return rep
    try:
        import jsonschema
        v = jsonschema.Draft7Validator(schema)
        errs = sorted(v.iter_errors(doc), key=lambda e: list(e.absolute_path))
        for e in errs[:50]:
            path = "/".join(str(p) for p in e.absolute_path)
            rep.errors.append(f"{path or '(root)'}: {e.message[:160]}")
        if len(errs) > 50:
            rep.errors.append(f"... and {len(errs) - 50} more schema errors")
    except ImportError:
        rep.warnings.append("jsonschema not installed: schema validation skipped")
    return rep


def _schema_for(version: str, schema_dir: Path | None = None):
    schema_dir = schema_dir or Path(__file__).resolve().parent.parent / "schemas"
    v = (version or "").strip().lstrip("vV")
    candidates = sorted(schema_dir.glob("V*_Hospital_price_transparency_schema.json"))
    exact = schema_dir / f"V{v}_Hospital_price_transparency_schema.json"
    if exact.exists():
        return json.loads(exact.read_text())
    major_minor = ".".join(v.split(".")[:2])
    same = [c for c in candidates if c.name[1:].startswith(major_minor)]
    if not same:
        same = [c for c in candidates if c.name[1:].startswith(v.split(".")[0] + ".")]
    return json.loads(same[-1].read_text()) if same else None


# ------------------------------------------------------------------ entry points

def validate_bytes(body: bytes, source: str, truncated: bool = False, max_rows: int = 2000) -> Report:
    rep = Report(source=source)
    name = source.rstrip("/").split("/")[-1].split("?")[0]
    from urllib.parse import unquote
    rep.filename_ok = bool(FILENAME_RE.match(unquote(name)))
    if not rep.filename_ok:
        rep.warnings.append("file name does not follow <ein>_<hospital-name>_standardcharges.<ext>")
    body, wrapper = http.maybe_decompress(body)
    if wrapper == "zip":
        rep.format = "zip"
        if truncated:
            rep.warnings.append("zip archive: content not inspected (head only)")
            return rep
        import zipfile
        with zipfile.ZipFile(io.BytesIO(body)) as z:
            inner = [n for n in z.namelist() if n.lower().endswith((".csv", ".json"))]
            if not inner:
                rep.errors.append("zip archive contains no .csv or .json file")
                return rep
            with z.open(inner[0]) as f:
                body = f.read(20_000_000)
            truncated = True
    text = http.decode_text(body)
    stripped = text.lstrip()[:500].lower()
    if stripped.startswith(("<!doctype", "<html")) or "<html" in stripped:
        rep.format = "html"
        rep.errors.append("URL returns an HTML page, not a machine-readable file")
        return rep
    if stripped.startswith(("{", "[")):
        rep = validate_json_text(text, rep, complete=not truncated)
    else:
        rep = validate_csv_text(text, rep, max_rows=max_rows, truncated=truncated)
    if rep.version.strip().lstrip("vV").startswith("2"):
        rep.warnings.append("outdated template: v2.x; CMS requires v3.0 from 2026-04-01")
    return rep


def validate_path(path: str | Path, max_bytes: int = 20_000_000, max_rows: int = 2000) -> Report:
    p = Path(path)
    with open(p, "rb") as f:
        body = f.read(max_bytes + 1)
    truncated = len(body) > max_bytes
    rep = validate_bytes(body[:max_bytes], str(p), truncated=truncated, max_rows=max_rows)
    rep.content_length = p.stat().st_size
    return rep
