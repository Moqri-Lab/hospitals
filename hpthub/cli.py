"""Command line interface: ``python -m hpthub <command>``."""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd

from . import discover, http, registry, validate

ROOT = registry.ROOT


def cmd_cms_download(a):
    registry.download_cms(Path(a.out))
    print(f"CMS files written to {a.out}")


def cmd_registry(a):
    df = registry.build(Path(a.cms_dir), min_beds=a.min_beds, acute_only=a.acute_only)
    p = registry.save(df)
    print(f"{len(df)} hospitals -> {p} ({(df['domains'] != '').sum()} with a candidate domain)")


def _targets(limit: int | None, include_exempt: bool) -> pd.DataFrame:
    reg = registry.load()
    if not include_exempt:
        reg = reg[reg["hpt_exempt"].astype(str).str.lower() != "true"]
    return reg.head(limit) if limit else reg


def cmd_discover(a):
    reg = _targets(a.limit, a.include_exempt)
    tmp = ROOT / "data" / "registry" / ".targets.csv"
    reg.to_csv(tmp, index=False)
    try:
        discover.run(tmp, workers=a.workers)
    finally:
        tmp.unlink(missing_ok=True)


def cmd_validate(a):
    if a.source:
        rep = (validate.validate_url(a.source) if a.source.startswith("http")
               else validate.validate_path(a.source))
        print(json.dumps(rep.as_row(), indent=2, default=str))
        sys.exit(0 if rep.status == "valid" else 1)
    m = pd.read_csv(discover.DISC_DIR / "hospital_mrfs.csv", dtype=str, keep_default_na=False)
    urls = sorted({u for u in m["mrf_url"] if u.startswith("http")})
    if a.limit:
        urls = urls[: a.limit]
    print(f"validating {len(urls)} MRF URLs (head of each file)")
    sess = http.session()
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        reps = list(ex.map(lambda u: validate.validate_url(u, sess, max_bytes=a.max_bytes), urls))
    out = pd.DataFrame([r.as_row() for r in reps])
    path = ROOT / "data" / "validation" / "mrf_validation.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(path, index=False)
    print(out["status"].value_counts().to_string())
    print(f"-> {path}")


def cmd_normalize(a):
    from . import normalize
    meta = {"ccn": a.ccn, "mrf_url": a.source if a.source.startswith("http") else None}
    with http.open_local_or_url(a.source) as f:
        n = normalize.normalize(f, Path(a.out), meta=meta, max_rows=a.max_rows)
    print(f"{n} rows -> {a.out}")


def cmd_site(a):
    from . import site
    p = site.build()
    print(f"site data -> {p}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="hpthub", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("cms-download", help="download CMS Hospital General Information + POS")
    s.add_argument("--out", default=str(registry.CMS_DIR))
    s.set_defaults(fn=cmd_cms_download)

    s = sub.add_parser("registry", help="build data/registry/hospitals.csv from CMS files")
    s.add_argument("--cms-dir", default=str(registry.CMS_DIR))
    s.add_argument("--min-beds", type=int, default=0)
    s.add_argument("--acute-only", action="store_true")
    s.set_defaults(fn=cmd_registry)

    s = sub.add_parser("discover", help="fetch cms-hpt.txt for the largest N hospitals")
    s.add_argument("--limit", type=int, default=100)
    s.add_argument("--workers", type=int, default=16)
    s.add_argument("--include-exempt", action="store_true", help="include VA/DoD hospitals")
    s.set_defaults(fn=cmd_discover)

    s = sub.add_parser("validate", help="validate one MRF (path/URL) or all discovered MRFs")
    s.add_argument("source", nargs="?")
    s.add_argument("--limit", type=int)
    s.add_argument("--workers", type=int, default=8)
    s.add_argument("--max-bytes", type=int, default=5_000_000)
    s.set_defaults(fn=cmd_validate)

    s = sub.add_parser("normalize", help="normalize one MRF (path/URL) to Parquet")
    s.add_argument("source")
    s.add_argument("--out", required=True)
    s.add_argument("--ccn")
    s.add_argument("--max-rows", type=int)
    s.set_defaults(fn=cmd_normalize)

    s = sub.add_parser("site", help="build site/data.json for the static browser")
    s.set_defaults(fn=cmd_site)

    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
