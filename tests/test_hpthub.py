from pathlib import Path

import pytest

from hpthub import hpttxt, validate, normalize
from hpthub.discover import name_score

FIX = Path(__file__).parent / "fixtures"


def test_parse_hpt_txt():
    txt = """location-name: West Mercy Hospital
source-page-url: https://westmercy.org/prices
mrf-url: https://westmercy.org/12-3456789_west-mercy_standardcharges.csv
contact-name: Jane Doe
contact-email: jane@westmercy.org

location-name: West Mercy Surgical Center
source-page-url: https://westmercy.org/prices
mrf-url: https://westmercy.org/12-3456789_west-mercy-surgical_standardcharges.json
contact-name: Jane Doe
contact-email: jane@westmercy.org
"""
    f = hpttxt.parse(txt)
    assert f.ok and len(f.entries) == 2
    assert f.entries[1].mrf_url.endswith(".json")
    assert not f.entries[0].issues


def test_parse_hpt_txt_rejects_html():
    assert not hpttxt.parse("<!DOCTYPE html><html>404</html>").ok


@pytest.mark.parametrize("name,fmt,version", [
    ("v3_tall.csv", "csv-tall", "3.0.0"), ("v3_wide.csv", "csv-wide", "3.0.0"), ("v3.json", "json", "3.0.0"),
    ("v2_tall.csv", "csv-tall", "2.0.0"), ("v2_wide.csv", "csv-wide", "2.0.0"), ("v2.json", "json", "2.0.0"),
])
def test_cms_examples_are_valid(name, fmt, version):
    r = validate.validate_path(FIX / name)
    assert (r.format, r.version, r.status) == (fmt, version, "valid"), r.errors


def test_invalid_csv_is_flagged():
    text = (FIX / "v3_tall.csv").read_text(encoding="utf-8-sig").replace(",outpatient,", ",OP,", 1)
    r = validate.validate_bytes(text.encode(), "x.csv")
    assert r.status == "invalid" and any("setting" in e for e in r.errors)


@pytest.mark.parametrize("name", ["v3_tall.csv", "v3_wide.csv", "v3.json"])
def test_normalize_same_totals(tmp_path, name):
    import pyarrow.parquet as pq
    out = tmp_path / "o.parquet"
    with open(FIX / name, "rb") as f:
        n = normalize.normalize(f, out)
    t = pq.read_table(out).to_pandas()
    assert n == len(t) > 0
    assert round(t["negotiated_dollar"].sum(), 2) == 112889.7
    assert set(t["payer_name"].dropna()) == {"Platform Health Insurance", "Region Health Insurance"}


def test_name_score():
    assert name_score("Barnes Jewish Hospital", "Barnes-Jewish Hospital") > 0.8
    assert name_score("Barnes Jewish Hospital", "Christian Hospital") < 0.5
