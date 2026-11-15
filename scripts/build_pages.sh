#!/usr/bin/env bash
# Build the GitHub Pages site: the `prod` branch at the root (the stable site), and `main` under
# /dev/ (the development version, updated continuously). Nothing is duplicated in the repository:
# prod is only a branch pointing at an older commit of main. Run from the repository root.
set -euo pipefail
rm -rf _site _prod
mkdir -p _site
if git fetch -q --depth=1 origin prod:refs/remotes/origin/prod 2>/dev/null; then
  git worktree add -q --detach _prod origin/prod
  PROD=_prod
else
  echo "no prod branch yet: publishing main as prod too"
  PROD=.
fi
copy() {  # copy() <source checkout> <destination>
  mkdir -p "$2"
  for f in index.html about.html exempt.html data.json CATALOG.md .nojekyll; do
    [ -e "$1/$f" ] && cp "$1/$f" "$2/"
  done
  cp -r "$1/data" "$2/"
  python3 scripts/dir_index.py "$2/data" >/dev/null
}
copy "$PROD" _site
copy . _site/dev
python3 - <<'P'
import pathlib, re
banner = ('<div style="background:#fdf0dc;color:#7a4300;padding:6px 16px;font:13px system-ui,sans-serif;'
          'text-align:center;border-bottom:1px solid #e9c98f">Development version, updated continuously. '
          'The stable site is <a href="../" style="color:inherit;font-weight:600">here</a>.</div>')
for p in pathlib.Path("_site/dev").glob("*.html"):
    s = p.read_text()
    s = s.replace("<head>", '<head><meta name="robots" content="noindex">', 1)
    s = re.sub(r"(<body[^>]*>)", r"\1" + banner, s, count=1)
    p.write_text(s)
P
[ "$PROD" = _prod ] && git worktree remove --force _prod
echo "prod: $(git rev-parse --short origin/prod 2>/dev/null || git rev-parse --short HEAD)  dev: $(git rev-parse --short HEAD)"
