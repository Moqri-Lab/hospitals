"""Normalize CMS-template MRFs (CSV tall, CSV wide, JSON; v2 and v3) into one
long table, one row per (item/service x setting x payer/plan), written to
Parquet so it can be queried with DuckDB, pandas, polars, R arrow, etc.
"""

from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path
from typing import Iterator

import pyarrow as pa
import pyarrow.parquet as pq

from .validate import norm_header

SCHEMA = pa.schema([
    ("ccn", pa.string()),
    ("hospital_name", pa.string()),
    ("last_updated_on", pa.string()),
    ("template_version", pa.string()),
    ("mrf_url", pa.string()),
    ("description", pa.string()),
    ("code", pa.string()),
    ("code_type", pa.string()),
    ("all_codes", pa.string()),          # "TYPE:code|TYPE:code" for every code on the line
    ("modifiers", pa.string()),
    ("setting", pa.string()),
    ("drug_unit", pa.float64()),
    ("drug_type", pa.string()),
    ("gross_charge", pa.float64()),
    ("discounted_cash", pa.float64()),
    ("deidentified_min", pa.float64()),
    ("deidentified_max", pa.float64()),
    ("payer_name", pa.string()),
    ("plan_name", pa.string()),
    ("methodology", pa.string()),
    ("negotiated_dollar", pa.float64()),
    ("negotiated_percentage", pa.float64()),
    ("negotiated_algorithm", pa.string()),
    ("estimated_amount", pa.float64()),   # v2 only
    ("median_amount", pa.float64()),      # v3
    ("p10_amount", pa.float64()),         # v3
    ("p90_amount", pa.float64()),         # v3
    ("allowed_count", pa.string()),       # v3
    ("notes", pa.string()),
])
COLS = SCHEMA.names
CODE_PRIORITY = ["CPT", "HCPCS", "MS-DRG", "APR-DRG", "DRG", "NDC", "RC", "CDM", "LOCAL"]


def num(v) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "").replace("$", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _primary_code(codes: list[tuple[str, str]]) -> tuple[str, str]:
    for t in CODE_PRIORITY:
        for c, ct in codes:
            if ct.upper() == t and c:
                return c, ct.upper()
    for c, ct in codes:
        if c:
            return c, ct.upper()
    return "", ""


def _empty_row(meta: dict) -> dict:
    r = {c: None for c in COLS}
    r.update(meta)
    return r


# ------------------------------------------------------------------ CSV

def iter_csv(f: io.TextIOBase, meta: dict) -> Iterator[dict]:
    reader = csv.reader(f)
    row1 = [norm_header(h) for h in next(reader)]
    row2 = next(reader)
    raw3 = next(reader)
    h3 = [norm_header(h) for h in raw3]
    m2 = {h: (row2[i].strip() if i < len(row2) else "") for i, h in enumerate(row1) if h}
    meta = dict(meta)
    meta.setdefault("hospital_name", m2.get("hospital_name"))
    meta["hospital_name"] = meta.get("hospital_name") or m2.get("hospital_name")
    meta["last_updated_on"] = m2.get("last_updated_on")
    meta["template_version"] = m2.get("version")
    idx = {h: i for i, h in enumerate(h3)}
    code_idx = sorted(int(m.group(1)) for h in h3 if (m := re.fullmatch(r"code\|(\d+)", h)))
    tall = "payer_name" in idx

    # wide: map (payer, plan) -> {field: col}
    plans: dict[tuple[str, str], dict[str, int]] = {}
    names: dict[tuple[str, str], tuple[str, str]] = {}
    if not tall:
        for h, i in idx.items():
            parts = h.split("|")
            rawp = [x.strip() for x in raw3[i].split("|")]
            if len(parts) in (3, 4) and len(rawp) == len(parts):
                names.setdefault((parts[1], parts[2]), (rawp[1], rawp[2]))
            if parts[0] == "standard_charge" and len(parts) == 4:
                plans.setdefault((parts[1], parts[2]), {})[parts[3]] = i
            elif parts[0] in ("median_amount", "10th_percentile", "90th_percentile", "count",
                              "estimated_amount", "additional_payer_notes") and len(parts) == 3:
                plans.setdefault((parts[1], parts[2]), {})[parts[0]] = i
    # recover original-case payer/plan names from the raw header row
    def g(row, key):
        i = idx.get(key)
        return row[i].strip() if i is not None and i < len(row) and row[i] is not None else ""

    for row in reader:
        if not any(c.strip() for c in row):
            continue
        codes = [(g(row, f"code|{k}"), g(row, f"code|{k}|type")) for k in code_idx]
        code, ctype = _primary_code(codes)
        base = _empty_row(meta)
        base.update(
            description=g(row, "description"), code=code, code_type=ctype,
            all_codes="|".join(f"{t.upper()}:{c}" for c, t in codes if c),
            modifiers=g(row, "modifiers") or None, setting=g(row, "setting").lower() or None,
            drug_unit=num(g(row, "drug_unit_of_measurement")),
            drug_type=g(row, "drug_type_of_measurement") or None,
            gross_charge=num(g(row, "standard_charge|gross")),
            discounted_cash=num(g(row, "standard_charge|discounted_cash")),
            deidentified_min=num(g(row, "standard_charge|min")),
            deidentified_max=num(g(row, "standard_charge|max")),
            notes=g(row, "additional_generic_notes") or None,
        )
        if tall:
            base.update(
                payer_name=g(row, "payer_name") or None, plan_name=g(row, "plan_name") or None,
                methodology=g(row, "standard_charge|methodology").lower() or None,
                negotiated_dollar=num(g(row, "standard_charge|negotiated_dollar")),
                negotiated_percentage=num(g(row, "standard_charge|negotiated_percentage")),
                negotiated_algorithm=g(row, "standard_charge|negotiated_algorithm") or None,
                estimated_amount=num(g(row, "estimated_amount")),
                median_amount=num(g(row, "median_amount")), p10_amount=num(g(row, "10th_percentile")),
                p90_amount=num(g(row, "90th_percentile")), allowed_count=g(row, "count") or None,
            )
            yield base
            continue
        emitted = False
        for (payer, plan), cols in plans.items():
            gv = lambda k: (row[cols[k]].strip() if k in cols and cols[k] < len(row) else "")  # noqa: E731
            if not any(gv(k) for k in ("negotiated_dollar", "negotiated_percentage", "negotiated_algorithm",
                                       "median_amount", "estimated_amount")):
                continue
            r = dict(base)
            notes = gv("additional_payer_notes")
            r.update(
                payer_name=names.get((payer, plan), (payer, plan))[0],
                plan_name=names.get((payer, plan), (payer, plan))[1], methodology=gv("methodology").lower() or None,
                negotiated_dollar=num(gv("negotiated_dollar")),
                negotiated_percentage=num(gv("negotiated_percentage")),
                negotiated_algorithm=gv("negotiated_algorithm") or None,
                estimated_amount=num(gv("estimated_amount")), median_amount=num(gv("median_amount")),
                p10_amount=num(gv("10th_percentile")), p90_amount=num(gv("90th_percentile")),
                allowed_count=gv("count") or None,
                notes="; ".join(x for x in (base["notes"], notes) if x) or None,
            )
            emitted = True
            yield r
        if not emitted:
            yield base


# ------------------------------------------------------------------ JSON

def iter_json(doc: dict, meta: dict) -> Iterator[dict]:
    meta = dict(meta)
    meta["hospital_name"] = meta.get("hospital_name") or doc.get("hospital_name")
    meta["last_updated_on"] = doc.get("last_updated_on")
    meta["template_version"] = doc.get("version")
    for item in doc.get("standard_charge_information", []) or []:
        codes = [(str(c.get("code", "")), str(c.get("type", ""))) for c in item.get("code_information", []) or []]
        code, ctype = _primary_code(codes)
        drug = item.get("drug_information") or {}
        for sc in item.get("standard_charges", []) or []:
            base = _empty_row(meta)
            base.update(
                description=item.get("description"), code=code, code_type=ctype,
                all_codes="|".join(f"{t.upper()}:{c}" for c, t in codes if c),
                modifiers="|".join(sc.get("modifier_code", []) or []) or None,
                setting=(sc.get("setting") or "").lower() or None,
                drug_unit=num(drug.get("unit")), drug_type=drug.get("type"),
                gross_charge=num(sc.get("gross_charge")), discounted_cash=num(sc.get("discounted_cash")),
                deidentified_min=num(sc.get("minimum")), deidentified_max=num(sc.get("maximum")),
                notes=sc.get("additional_generic_notes"),
            )
            payers = sc.get("payers_information") or []
            if not payers:
                yield base
            for p in payers:
                r = dict(base)
                r.update(
                    payer_name=p.get("payer_name"), plan_name=p.get("plan_name"),
                    methodology=(p.get("methodology") or "").lower() or None,
                    negotiated_dollar=num(p.get("standard_charge_dollar")),
                    negotiated_percentage=num(p.get("standard_charge_percentage")),
                    negotiated_algorithm=p.get("standard_charge_algorithm"),
                    estimated_amount=num(p.get("estimated_amount")),
                    median_amount=num(p.get("median_amount")), p10_amount=num(p.get("10th_percentile")),
                    p90_amount=num(p.get("90th_percentile")),
                    allowed_count=str(p["count"]) if p.get("count") is not None else None,
                    notes="; ".join(x for x in (base["notes"], p.get("additional_payer_notes")) if x) or None,
                )
                yield r


# ------------------------------------------------------------------ driver

def normalize(fileobj, out_path: Path, meta: dict | None = None, max_rows: int | None = None,
              batch_size: int = 50_000) -> int:
    """Read an MRF from a binary file object and write normalized Parquet. Returns row count."""
    meta = {k: v for k, v in (meta or {}).items() if k in COLS}
    raw = io.BufferedReader(fileobj) if not hasattr(fileobj, "peek") else fileobj
    head = raw.peek(4096)[:4096].lstrip(b"\xef\xbb\xbf \r\n\t")
    if head[:1] in (b"{", b"["):
        doc = json.load(io.TextIOWrapper(raw, encoding="utf-8-sig"))
        rows = iter_json(doc, meta)
    else:
        text = io.TextIOWrapper(raw, encoding="utf-8-sig", errors="replace", newline="")
        rows = iter_csv(text, meta)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    writer = pq.ParquetWriter(out_path, SCHEMA, compression="zstd")
    n, buf = 0, []
    try:
        for r in rows:
            buf.append(r)
            n += 1
            if len(buf) >= batch_size:
                writer.write_table(pa.Table.from_pylist(buf, schema=SCHEMA))
                buf = []
            if max_rows and n >= max_rows:
                break
        if buf or n == 0:
            writer.write_table(pa.Table.from_pylist(buf, schema=SCHEMA))
    finally:
        writer.close()
    return n
