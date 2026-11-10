"""Turn existing open datasets into *candidate* website domains (leads).

Leads only fill hospitals that have no domain from the system rules or
overrides, and nothing enters the catalog unless the hospital's own live
cms-hpt.txt confirms it. CMS stays the source of the hospital list.

Currently supported:
  tpafs  DoltHub/TPAFS transparency-data machine_readable_links.csv (CC-BY-SA-4.0,
         https://github.com/TPAFS/transparency-data); only the hostnames of the
         hospital pages are used.

Usage: python scripts/import_domain_leads.py tpafs path/to/machine_readable_links.csv
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


def main():
    src, path = sys.argv[1], sys.argv[2]
    leads = {"tpafs": tpafs}[src](path)
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
