"""Turn existing open datasets into *candidate* website domains (leads).

Leads only fill hospitals that have no domain from the system rules or
overrides, and nothing enters the catalog unless the hospital's own live
cms-hpt.txt confirms it. CMS stays the source of the hospital list.

Currently supported:
  tpafs  DoltHub/TPAFS transparency-data machine_readable_links.csv (CC-BY-SA-4.0,
         https://github.com/TPAFS/transparency-data); only the hostnames of the
         hospital pages are used.

  sutton nathansutton/hospital-price-transparency dim/urls/*.json (Apache-2.0 repo,
         https://github.com/nathansutton/hospital-price-transparency);
         hostnames of transparency_page/file_url.

  wikidata  Wikidata (CC0, https://www.wikidata.org): US hospitals with an
         "official website" (P856), fetched live from the Wikidata Query Service.
         Each item is matched to a CMS hospital by name plus city or ZIP; the
         matched Wikidata item IDs are saved in data/registry/wikidata_hospitals.csv
         so every lead can be traced back to its Wikidata page.

  osm    OpenStreetMap (ODbL, https://www.openstreetmap.org/copyright): hospitals
         (amenity=hospital / healthcare=hospital) in the US and Puerto Rico with a
         website tag, from the Overpass API. Each one is matched to a CMS hospital by
         name within 25 km (CMS ZIP located with the Census ZCTA gazetteer) or by
         its address ZIP; ambiguous matches are dropped. Matched OSM objects are
         saved in data/registry/osm_hospitals.csv.

Usage: python scripts/import_domain_leads.py tpafs path/to/machine_readable_links.csv
       python scripts/import_domain_leads.py sutton path/to/dim/urls
       python scripts/import_domain_leads.py wikidata
       python scripts/import_domain_leads.py osm
"""

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from hpthub.registry import REG_DIR, candidate_hosts, base_domain  # noqa: E402


def tpafs(path: str) -> dict[str, str]:
    out: dict[str, list[str]] = {}
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            ccn = (r.get("ccn") or "").strip().zfill(6)
            if ccn == "000000":
                continue
            hosts = candidate_hosts(r.get("machine_readable_page", ""), r.get("machine_readable_url", ""))
            doms = []
            for h in hosts:
                b = base_domain(h)
                if b not in doms:
                    doms.append(b)
            if doms:
                out.setdefault(ccn, [])
                out[ccn] += [d for d in doms if d not in out[ccn]]
    return {k: " ".join(v[:3]) for k, v in out.items()}


def sutton(path: str) -> dict[str, str]:
    import json
    out: dict[str, list[str]] = {}
    for f in sorted(Path(path).glob("*.json")):
        for r in json.loads(f.read_text()):
            ccn = str(r.get("ccn") or "").strip().zfill(6)
            if ccn == "000000":
                continue
            for h in candidate_hosts(r.get("transparency_page", ""), r.get("file_url", "")):
                b = base_domain(h)
                lst = out.setdefault(ccn, [])
                if b not in lst:
                    lst.append(b)
    return {k: " ".join(v[:3]) for k, v in out.items() if v}


WIKIDATA_SPARQL = """
SELECT ?h ?hLabel ?site ?cityLabel ?zip WHERE {
  ?h wdt:P31/wdt:P279* wd:Q16917 ;
     wdt:P17 wd:Q30 ;
     wdt:P856 ?site .
  OPTIONAL { ?h wdt:P131 ?city }
  OPTIONAL { ?h wdt:P281 ?zip }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
"""


def _norm_city(s: str) -> str:
    s = (s or "").lower().replace("saint ", "st ").replace("st. ", "st ")
    return " ".join(s.replace(",", " ").split())


def wikidata(_path: str = "") -> dict[str, str]:
    import pandas as pd
    import requests
    from hpthub.discover import name_score
    from hpthub.registry import load_cms

    r = requests.get("https://query.wikidata.org/sparql",
                     params={"query": WIKIDATA_SPARQL, "format": "json"},
                     headers={"User-Agent": "hpt-hub/0.1 (open hospital price transparency research; "
                                            "+https://github.com/Moqri-Lab/hospitals)",
                              "Accept": "application/sparql-results+json"}, timeout=300)
    r.raise_for_status()
    items: dict[str, dict] = {}
    for b in r.json()["results"]["bindings"]:
        qid = b["h"]["value"].rsplit("/", 1)[-1]
        it = items.setdefault(qid, {"qid": qid, "label": b.get("hLabel", {}).get("value", ""),
                                    "sites": [], "cities": set(), "zips": set()})
        if b["site"]["value"] not in it["sites"]:
            it["sites"].append(b["site"]["value"])
        if "cityLabel" in b:
            it["cities"].add(_norm_city(b["cityLabel"]["value"]))
        if "zip" in b:
            it["zips"].add(b["zip"]["value"][:5])
    print(f"{len(items)} Wikidata US hospitals with an official website")

    cms = load_cms()
    cms["city_n"] = cms["city"].map(_norm_city)
    cms["zip5"] = cms["zip"].astype(str).str[:5]
    by_city = {k: g for k, g in cms.groupby("city_n")}
    by_zip = {k: g for k, g in cms.groupby("zip5")}

    best: dict[str, tuple[float, dict, list[str]]] = {}
    for it in items.values():
        if not it["label"] or it["label"] == it["qid"]:
            continue
        doms = []
        for h in candidate_hosts(*it["sites"]):
            d = base_domain(h)
            if d not in doms:
                doms.append(d)
        if not doms:
            continue
        cands = [by_city[c] for c in it["cities"] if c in by_city] + [by_zip[z] for z in it["zips"] if z in by_zip]
        if not cands:
            continue
        pool = pd.concat(cands).drop_duplicates("ccn")
        scored = sorted(((name_score(it["label"], n), c) for c, n in zip(pool["ccn"], pool["name"])), reverse=True)
        top, ccn = scored[0]
        # confident and unambiguous within the same city/ZIP
        if top < 0.75 or (len(scored) > 1 and scored[1][0] >= top - 0.05):
            continue
        if ccn not in best or top > best[ccn][0]:
            best[ccn] = (top, it, doms)

    rows = [{"ccn": c, "wikidata_id": it["qid"], "wikidata_label": it["label"],
             "wikidata_url": f"https://www.wikidata.org/wiki/{it['qid']}",
             "official_website": " ".join(it["sites"]), "match_score": round(s, 3)}
            for c, (s, it, _) in sorted(best.items())]
    out = REG_DIR / "wikidata_hospitals.csv"
    with out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]) if rows else ["ccn"])
        w.writeheader()
        w.writerows(rows)
    print(f"{len(rows)} CMS hospitals matched to Wikidata -> {out}")
    return {c: " ".join(d[:3]) for c, (_, _, d) in best.items()}


OVERPASS = """[out:json][timeout:900];
(area["ISO3166-1"="US"][admin_level=2];area["ISO3166-1"="PR"][admin_level=2];)->.a;
(nwr["amenity"="hospital"]["website"](area.a);nwr["amenity"="hospital"]["contact:website"](area.a);
 nwr["healthcare"="hospital"]["website"](area.a);nwr["healthcare"="hospital"]["contact:website"](area.a););
out center tags;"""
OVERPASS_ENDPOINTS = ("https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter",
                      "https://overpass.private.coffee/api/interpreter")
ZCTA_URL = "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2024_Gazetteer/2024_Gaz_zcta_national.zip"
UA = {"User-Agent": "hpt-hub/0.1 (open hospital price transparency research; +https://github.com/Moqri-Lab/hospitals)"}


def zip_centroids() -> dict[str, tuple[float, float]]:
    import io
    import zipfile
    import requests
    r = requests.get(ZCTA_URL, headers=UA, timeout=300)
    r.raise_for_status()
    z = zipfile.ZipFile(io.BytesIO(r.content))
    txt = z.read(z.namelist()[0]).decode("latin-1").splitlines()
    head = [h.strip() for h in txt[0].split("\t")]
    i, la, lo = head.index("GEOID"), head.index("INTPTLAT"), head.index("INTPTLONG")
    out = {}
    for line in txt[1:]:
        c = line.split("\t")
        try:
            out[c[i].strip()] = (float(c[la]), float(c[lo]))
        except (ValueError, IndexError):
            pass
    return out


def _km(a, b) -> float:
    import math
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 12742 * math.asin(math.sqrt(h))


def osm(_path: str = "") -> dict[str, str]:
    import requests
    from hpthub.discover import name_score
    from hpthub.registry import load_cms

    import time
    els = None
    for attempt in range(3):
        for ep in OVERPASS_ENDPOINTS:
            try:
                r = requests.post(ep, data={"data": OVERPASS}, headers=UA, timeout=1000)
                r.raise_for_status()
                els = r.json()["elements"]
                break
            except Exception as ex:  # busy or rate-limited mirror: try the next one
                print(f"{ep}: {ex}")
        if els is not None:
            break
        time.sleep(60 * (attempt + 1))
    if els is None:
        raise SystemExit("no Overpass server answered")
    print(f"{len(els)} OpenStreetMap hospitals with a website")
    zc = zip_centroids()
    cms = load_cms()
    cms["zip5"] = cms["zip"].astype(str).str[:5]
    pts = [(c, n, zc.get(z)) for c, n, z in zip(cms["ccn"], cms["name"], cms["zip5"])]
    by_zip: dict[str, list] = {}
    for c, n, z in zip(cms["ccn"], cms["name"], cms["zip5"]):
        by_zip.setdefault(z, []).append((c, n))
    best: dict[str, tuple[float, dict, list[str]]] = {}
    for e in els:
        t = e.get("tags", {})
        names = [t.get(k, "") for k in ("name", "official_name", "alt_name", "old_name") if t.get(k)]
        if not names:
            continue
        lat, lon = (e.get("lat"), e.get("lon")) if "lat" in e else (e.get("center", {}).get("lat"), e.get("center", {}).get("lon"))
        site = t.get("website") or t.get("contact:website") or ""
        doms = []
        for h in candidate_hosts(site if "://" in site else "https://" + site):
            d = base_domain(h)
            if d not in doms:
                doms.append(d)
        if not doms:
            continue
        pool = {}
        if lat is not None:
            for c, n, p in pts:
                if p and abs(p[0] - lat) < 0.3 and abs(p[1] - lon) < 0.4 and _km(p, (lat, lon)) <= 25:
                    pool[c] = n
        for c, n in by_zip.get(str(t.get("addr:postcode", ""))[:5], []):
            pool[c] = n
        if not pool:
            continue
        scored = sorted(((max(name_score(x, n) for x in names), c) for c, n in pool.items()), reverse=True)
        top, ccn = scored[0]
        if top < 0.75 or (len(scored) > 1 and scored[1][0] >= top - 0.05):
            continue
        it = {"osm": f"{e['type']}/{e['id']}", "name": names[0], "site": site}
        if ccn not in best or top > best[ccn][0]:
            best[ccn] = (top, it, doms)
    rows = [{"ccn": c, "osm_id": it["osm"], "osm_name": it["name"],
             "osm_url": f"https://www.openstreetmap.org/{it['osm']}", "website": it["site"],
             "match_score": round(s, 3)} for c, (s, it, _) in sorted(best.items())]
    out = REG_DIR / "osm_hospitals.csv"
    with out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]) if rows else ["ccn"])
        w.writeheader()
        w.writerows(rows)
    print(f"{len(rows)} CMS hospitals matched to OpenStreetMap -> {out}")
    return {c: " ".join(d[:3]) for c, (_, _, d) in best.items()}


def main():
    src = sys.argv[1]
    path = sys.argv[2] if len(sys.argv) > 2 else ""
    leads = {"tpafs": tpafs, "sutton": sutton, "wikidata": wikidata, "osm": osm}[src](path)
    p = REG_DIR / "domain_leads.csv"
    rows = {}
    if p.exists():
        with p.open(newline="") as f:
            rows = {(r["ccn"], r["source"]): r for r in csv.DictReader(f)}
    for ccn, doms in leads.items():
        rows[(ccn, src)] = {"ccn": ccn, "domains": doms, "source": src}
    with p.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["ccn", "domains", "source"])
        w.writeheader()
        for k in sorted(rows):
            w.writerow(rows[k])
    print(f"{len(leads)} {src} leads -> {p}")


if __name__ == "__main__":
    main()
