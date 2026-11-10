"""Command line interface: ``python -m hpthub <command>``."""

from __future__ import annotations

import argparse
import json
import sys
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


def _local_only(src: str) -> str:
    if src.startswith(("http://", "https://")):
        sys.exit("hpthub never downloads price files: download the MRF yourself and pass a local path")
    return src


def cmd_validate(a):
    rep = validate.validate_path(_local_only(a.source))
    print(json.dumps(rep.as_row(), indent=2, default=str))
    sys.exit(0 if rep.status == "valid" else 1)


def cmd_normalize(a):
    from . import normalize
    with http.open_local(_local_only(a.source)) as f:
        n = normalize.normalize(f, Path(a.out), meta={"ccn": a.ccn, "mrf_url": a.mrf_url},
                                max_rows=a.max_rows)
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

    s = sub.add_parser("validate", help="validate a local MRF file against the CMS template")
    s.add_argument("source", help="local path (price files are never downloaded)")
    s.set_defaults(fn=cmd_validate)

    s = sub.add_parser("normalize", help="normalize a local MRF file to Parquet")
    s.add_argument("source", help="local path (price files are never downloaded)")
    s.add_argument("--out", required=True)
    s.add_argument("--ccn")
    s.add_argument("--mrf-url", help="original URL, stored as metadata only")
    s.add_argument("--max-rows", type=int)
    s.set_defaults(fn=cmd_normalize)

    s = sub.add_parser("site", help="build site/data.json for the static browser")
    s.set_defaults(fn=cmd_site)

    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
