import pandas as pd

from hpthub import hpttxt
from hpthub.discover import match, mrf_format, name_score


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


def test_parse_hpt_txt_lenient():
    txt = "Location_Name = A Hospital\r\nMRF-URL: https://a.org/x.zip\r\nlocation-name: B Hospital\r\nmrf-url: https://a.org/y.csv"
    f = hpttxt.parse(txt)
    assert [e.location_name for e in f.entries] == ["A Hospital", "B Hospital"]
    assert "missing contact-name" in f.entries[0].issues


def test_parse_hpt_txt_rejects_html():
    assert not hpttxt.parse("<!DOCTYPE html><html>404</html>").ok


def test_mrf_format():
    assert mrf_format("https://a.org/12_x_standardcharges.json") == "json"
    assert mrf_format("https://a.org/files/x%20standardcharges.CSV?sv=1") == "csv"
    assert mrf_format("https://a.org/download?id=7", "application/zip") == "zip"


def test_name_score():
    assert name_score("Barnes Jewish Hospital", "Barnes-Jewish Hospital") > 0.8
    assert name_score("Barnes Jewish Hospital", "Christian Hospital") < 0.5


def test_match_picks_best_location():
    reg = pd.DataFrame([{"ccn": "260032", "name": "Barnes Jewish Hospital", "state": "MO", "beds": "1737",
                         "domains": "bjc.org"}])
    doms = pd.DataFrame([{"domain": "bjc.org", "found": True}])
    ent = pd.DataFrame([
        {"domain": "bjc.org", "location_name": "Christian Hospital", "mrf_url": "u1", "source_page_url": ""},
        {"domain": "bjc.org", "location_name": "Barnes-Jewish Hospital", "mrf_url": "u2", "source_page_url": ""},
    ])
    m = match(reg, doms, ent)
    assert list(m["mrf_url"]) == ["u2"] and m.loc[0, "match_status"] == "matched"


def test_match_respects_other_hospitals_on_domain():
    reg = pd.DataFrame([
        {"ccn": "330101", "name": "New York-Presbyterian Hospital", "state": "NY", "beds": "2262", "domains": "nyp.org"},
        {"ccn": "330055", "name": "New York-Presbyterian/Queens", "state": "NY", "beds": "535", "domains": "nyp.org"},
    ])
    doms = pd.DataFrame([{"domain": "nyp.org", "found": True}])
    ent = pd.DataFrame([
        {"domain": "nyp.org", "location_name": "NewYork-Presbyterian Weill Cornell Medical Center", "mrf_url": "main", "source_page_url": ""},
        {"domain": "nyp.org", "location_name": "NewYork-Presbyterian Queens", "mrf_url": "queens", "source_page_url": ""},
    ])
    m = match(reg.head(1), doms, ent, universe=reg)
    assert list(m["mrf_url"]) == ["main"]
