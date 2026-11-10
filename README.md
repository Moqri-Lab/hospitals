# Hospital price file catalog

An open, research-oriented catalog of where every US hospital publishes its
price transparency files.

For each CMS-certified hospital, the catalog lists:

- **Hospital info** from CMS: CCN, name, address, type, ownership, bed count
- **Every field of its `cms-hpt.txt`**: location name, source page URL,
  price-file (MRF) URL, contact name and contact email
- **Price-file metadata**: format, size, last-modified date and HTTP status,
  taken from an HTTP `HEAD` request

**This project never downloads price data.** It lists the links. Researchers
can fetch the files they need themselves.

| Output | What it is |
|---|---|
| [`CATALOG.md`](CATALOG.md) | Crawled hospitals as a readable table |
| [`data/catalog.csv`](data/catalog.csv) | Full catalog, one row per hospital × price file |
| [`site/index.html`](site/index.html) | Searchable, sortable table (GitHub Pages) |
| [`data/registry/hospitals.csv`](data/registry/hospitals.csv) | All 5,419 CMS hospitals, largest first |
| [`data/discovery/`](data/discovery) | Raw crawl results: domains checked, every `cms-hpt.txt` entry |

## Sources

| What | Source | Link |
|---|---|---|
| Hospital list (5,419 hospitals, CCN, name, address, type, ownership) | CMS Hospital General Information, data.cms.gov dataset `xubh-q36u`, July 2026 release | [dataset](https://data.cms.gov/provider-data/dataset/xubh-q36u) · [CSV](https://data.cms.gov/provider-data/sites/default/files/resources/893c372430d9d71a1c52737d01239d47_1785189955/Hospital_General_Information.csv) · [dictionary](https://data.cms.gov/provider-data/sites/default/files/data_dictionaries/hospital/HOSPITAL_Data_Dictionary.pdf) |
| Bed count (`BED_CNT`, total beds, active providers) | CMS Provider of Services file, hospital extract, Q3 2026 (2026-07-01 to 2026-09-30) | [search](https://data.cms.gov/search?keywords=Provider%20of%20Services%20File) · [CSV](https://data.cms.gov/sites/default/files/2026-10/e5a8afbb-a1f4-4d3b-bdb9-3968e194da0e/Hospital_and_other.DATA.Q3_2026.csv) |
| Price-file links, locations, contacts | Each hospital's own `cms-hpt.txt` (required by CMS since 2025) | [45 CFR 180](https://www.ecfr.gov/current/title-45/subtitle-A/subchapter-E/part-180) · [CMS HPT](https://www.cms.gov/priorities/key-initiatives/hospital-price-transparency) · [CMS spec](https://github.com/CMSgov/hospital-price-transparency) |
| cms-hpt.txt when the live site refuses automated access | Latest public archive copy: [Internet Archive](https://web.archive.org) or [Common Crawl](https://commoncrawl.org), dated and linked in the catalog (`hpt_txt_source`, `hpt_txt_url`); bot protection is never bypassed. The refusal itself is recorded in `hpt_txt_access_issue` (CMS requires the file for automated discovery, 45 CFR 180.50). | — |
| Price-file format, size, date | HTTP `HEAD` of each price-file URL (no download) | — |
| Website leads (candidates only, verified live) | Health-system name rules; official websites (P856) from Wikidata (CC0); hostnames from DoltHub/TPAFS transparency-data (CC BY-SA 4.0) and nathansutton/hospital-price-transparency (Apache-2.0; URLs via hospitalpricingfiles.org, Jan 2026) | [rules](data/registry/system_domains.csv) · [Wikidata](https://www.wikidata.org) · [matches](data/registry/wikidata_hospitals.csv) · [TPAFS](https://github.com/TPAFS/transparency-data) · [nathansutton](https://github.com/nathansutton/hospital-price-transparency) · [hospitalpricingfiles.org](https://hospitalpricingfiles.org) |

**Beds are per CCN.** CMS reports beds for each certification number, and one
CCN can cover several campuses (AdventHealth Orlando's 3,289 beds span
multiple campuses). The catalog's `n_campuses` column counts the distinct
`cms-hpt.txt` location names attached to each CCN, and "largest first" means
largest CCN by POS `BED_CNT`.

VA and DoD hospitals are marked `hpt_exempt`, because federal hospitals are
deemed compliant (45 CFR 180.30(b)). They are skipped by default when
crawling.

No official source lists hospital websites. The domain for each hospital
comes from [`data/registry/system_domains.csv`](data/registry/system_domains.csv)
(health-system rules) or [`data/registry/domains.csv`](data/registry/domains.csv)
(per-CCN overrides). A domain counts only when its `cms-hpt.txt` loads and one
of its `location-name` entries matches the CMS hospital name (`match_status`:
`matched`, `low-confidence`, `no-hpt-txt`, `no-domain`).

### Leads from existing databases

Existing open datasets are used only as **leads** for website domains
([`data/registry/domain_leads.csv`](data/registry/domain_leads.csv), built by
`scripts/import_domain_leads.py`). A lead never goes into the catalog by
itself: it is kept only if the hospital's own live `cms-hpt.txt` confirms it.
Current lead sources: the "official website" (P856) of US hospital items in
[Wikidata](https://www.wikidata.org), the open knowledge base behind Wikipedia
(CC0), fetched from the [Wikidata Query Service](https://query.wikidata.org) and
matched to a CMS hospital by name plus city or ZIP (each match, with its
Wikidata link, is in
[`data/registry/wikidata_hospitals.csv`](data/registry/wikidata_hospitals.csv));
hostnames from the DoltHub/TPAFS
[transparency-data](https://github.com/TPAFS/transparency-data) hospital links
(CC-BY-SA-4.0) and from
[nathansutton/hospital-price-transparency](https://github.com/nathansutton/hospital-price-transparency)
(Apache-2.0; its URL list comes from [hospitalpricingfiles.org](https://hospitalpricingfiles.org)).

### Confidence

`confidence` (0-100) in `data/catalog.csv` says how sure the catalog is that a
price file belongs to the CMS hospital. Base, by match method: name match =
name similarity x 100; reviewed by Claude (`matched-manual`) = 85; only entry
naming the hospital's city (`matched-city`) = 70; only hospital on that website
(`matched-sole`) = 65; `low-confidence` = similarity x 60. Plus 5 for each extra
independent source giving the same website (up to +15); minus 10 if the
cms-hpt.txt came from a public archive, minus 5 if from a saved copy.
`confidence_level`: high >= 80, medium 60-79, low < 60. See `hpthub/site.py`.

## Run it

```bash
pip install -e .
python -m hpthub cms-download          # refresh CMS files into data/cms/
python -m hpthub registry              # build data/registry/hospitals.csv
python -m hpthub discover --limit 100  # crawl cms-hpt.txt for the 100 largest
python -m hpthub site                  # write data/catalog.csv, CATALOG.md, site/data.json
```

The **discover** GitHub Action runs this weekly and on demand
(Actions → discover → Run workflow, with a `limit`).

## Contributing

To fix a missing or wrong hospital website, add a row `ccn,domains` to
`data/registry/domains.csv` and open a PR. The next crawl verifies it.

Code is MIT-licensed. CMS data is public domain.
