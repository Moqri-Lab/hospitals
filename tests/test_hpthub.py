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


def test_name_cleaning_and_abbreviations():
    assert name_score("Uh Cleveland Medical Center", "University Hospitals Cleveland Medical Center") >= 0.8
    assert name_score("Ochsner Medical Center Acute",
                      "Ochsner Medical Center - (This file encompasses multiple facilities, including OMC – Jeff Hwy)") >= 0.7


def test_sole_hospital_on_domain_gets_unclaimed_entries():
    reg = pd.DataFrame([{"ccn": "999999", "name": "Kaleida Health", "state": "NY", "beds": "1", "domains": "kaleidahealth.org"}])
    doms = pd.DataFrame([{"domain": "kaleidahealth.org", "found": True}])
    ent = pd.DataFrame([{"domain": "kaleidahealth.org", "location_name": "Buffalo General Medical Center",
                         "mrf_url": "u", "source_page_url": ""}])
    m = match(reg, doms, ent)
    assert m.loc[0, "match_status"] == "matched-sole"


def test_hospital_score_city_bonus():
    from hpthub.discover import hospital_score, name_score
    base = name_score("Beaumont Hospital - Dearborn", "Corewell Health Dearborn")
    assert hospital_score("Beaumont Hospital - Dearborn", "Dearborn", "Corewell Health Dearborn") > base


def test_plausible_size():
    from hpthub.discover import plausible_size
    assert not plausible_size(20, "application/octet-stream")
    assert not plausible_size(50_000, "text/html")
    assert plausible_size(5_000_000, "text/csv")
    assert not plausible_size(None)


def test_mrf_format_uses_server_filename():
    from hpthub.discover import mrf_format
    assert mrf_format("https://x.org/MRFDownload/abc", "application/octet-stream", "123_x_standardcharges.json") == "json"
    assert mrf_format("https://x.org/f.ashx", "", "", "https://cdn.x.org/a/b.csv") == "csv"


def _row(**kw):
    base = dict(ccn="1", match_status="matched", match_score="0.9", hpt_txt_domain="h.org", hpt_txt_source="live",
                hpt_txt_access_issue="", mrf_url="https://h.org/a.csv", mrf_http_status="200.0",
                web_system_rule="h.org", web_wikidata="h.org", web_tpafs="", web_hpf="", web_claude="")
    base.update(kw)
    return base


def test_confidence_levels():
    from hpthub.site import confidence
    v, lvl = confidence(_row())
    assert lvl == "high" and int(v) == 95  # 90 + 5 for a second agreeing source
    v, lvl = confidence(_row(match_status="low-confidence", match_score="0.3"))
    assert lvl == "low"
    v, _ = confidence(_row(hpt_txt_source="Internet Archive 2026-01-01"))
    assert int(v) == 85


def test_txt_check():
    import pandas as pd
    from hpthub.site import confidence, txt_check

    def run(**kw):
        g = pd.DataFrame([_row(**kw)])
        g["confidence"] = [confidence(r)[0] for _, r in g.iterrows()]
        return txt_check(g)[0]

    assert run() == "pass"
    assert run(mrf_http_status="404.0") == "fail"
    assert run(match_status="blocked", mrf_url="", hpt_txt_access_issue="refused automated access (HTTP 403)") == "blocked"
    assert run(match_status="no-hpt-txt", mrf_url="", hpt_txt_access_issue="no cms-hpt.txt (HTTP 404)") == "fail"
    # a single source for the website is not enough to blame the hospital
    assert run(match_status="no-hpt-txt", mrf_url="", web_wikidata="", hpt_txt_access_issue="no cms-hpt.txt (HTTP 404)") == "unknown"
    assert run(match_status="no-domain", mrf_url="", hpt_txt_domain="") == "unknown"


def test_utf16_cms_hpt_txt():
    from hpthub import http, hpttxt
    t = "location-name: A\r\nsource-page-url: https://a\r\nmrf-url: https://a/x.csv\r\ncontact-name: B\r\ncontact-email: b@a\r\n"
    for body in (t.encode("utf-16"), t.encode("utf-16-le")):
        assert len(hpttxt.parse(http.decode_text(body)).entries) == 1


def test_rival_tiebreak_by_distinctive_words():
    from hpthub.discover import loc_cover, common_words
    common = common_words(["Ascension Saint Thomas Rutherford", "Ascension Saint Thomas Midtown", "Ascension Via Christi"])
    loc = "Ascension Saint Thomas Rutherford"
    assert loc_cover("Saint Thomas Rutherford Hospital", "Murfreesboro", loc, common) > \
        loc_cover("Ascension Saint Thomas Hospital", "Nashville", loc, common)


def test_dead_hosts():
    import pandas as pd
    from hpthub.site import dead_hosts
    df = pd.DataFrame({"mrf_url": [f"https://x.org/{i}.csv" for i in range(5)] + ["https://y.org/a.csv", "https://y.org/b.csv"],
                       "mrf_http_status": ["404.0"] * 5 + ["404.0", "200.0"]})
    assert dead_hosts(df) == {"x.org"}
