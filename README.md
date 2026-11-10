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

All hospital data comes from official CMS releases:

- **Hospital General Information** ([data.cms.gov `xubh-q36u`](https://data.cms.gov/provider-data/dataset/xubh-q36u)):
  the list of Medicare-certified hospitals.
- **Provider of Services file, hospital extract** (data.cms.gov, latest
  quarter): bed counts, joined on CCN, active providers only.
- **`cms-hpt.txt`**: since 2025, every hospital must publish this file at the
  root of its website ([CMS guidance](https://www.cms.gov/hospital-price-transparency/resources)).
  It lists each location's price file and a contact.

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
Current lead source: hostnames from the DoltHub/TPAFS
[transparency-data](https://github.com/TPAFS/transparency-data) hospital links
(CC-BY-SA-4.0).

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
