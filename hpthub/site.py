"""Build ``site/data.json`` for the static hospital browser (site/index.html)."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pandas as pd

from . import registry

ROOT = registry.ROOT


def _read(p: Path) -> pd.DataFrame:
    return pd.read_csv(p, dtype=str, keep_default_na=False) if p.exists() else pd.DataFrame()


def build(out: Path | None = None) -> Path:
    reg = registry.load()
    mrfs = _read(ROOT / "data" / "discovery" / "hospital_mrfs.csv")
    val = _read(ROOT / "data" / "validation" / "mrf_validation.csv")

    if not mrfs.empty:
        keep = ["ccn", "hpt_txt_domain", "hpt_txt_found", "location_name", "mrf_url", "source_page_url",
                "match_score", "match_status"]
        reg = reg.merge(mrfs[keep], on="ccn", how="left")
    if not val.empty:
        v = val.rename(columns={"source": "mrf_url"})[
            ["mrf_url", "status", "format", "version", "last_updated_on", "n_errors", "n_warnings",
             "errors", "warnings", "n_payer_plans", "content_length"]]
        v = v.rename(columns={"status": "mrf_status", "format": "mrf_format", "version": "mrf_version",
                              "errors": "mrf_errors", "warnings": "mrf_warnings"})
        reg = reg.merge(v, on="mrf_url", how="left")
    reg = reg.fillna("")
    cols = [c for c in ["ccn", "name", "city", "state", "facility_type", "ownership", "beds", "system",
                        "hpt_exempt", "hpt_txt_domain", "hpt_txt_found", "location_name", "mrf_url",
                        "source_page_url", "match_status", "mrf_status", "mrf_format", "mrf_version",
                        "last_updated_on", "n_payer_plans", "content_length", "n_errors", "mrf_errors",
                        "mrf_warnings"] if c in reg.columns]
    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "columns": cols,
        "rows": reg[cols].values.tolist(),
    }
    out = out or ROOT / "site" / "data.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, separators=(",", ":")))
    return out
