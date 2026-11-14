"""Write an index.html listing into every folder under a directory, because
GitHub Pages does not list folders. Usage: python scripts/dir_index.py data"""

import html
import sys
from pathlib import Path


def fmt(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def write(root: Path) -> None:
    for d in [root, *sorted(p for p in root.rglob("*") if p.is_dir())]:
        rel = d.relative_to(root.parent)
        rows = []
        if d != root:
            rows.append('<tr><td><a href="../">../</a></td><td></td></tr>')
        for p in sorted(d.iterdir(), key=lambda p: (p.is_file(), p.name)):
            if p.name == "index.html":
                continue
            name = p.name + ("/" if p.is_dir() else "")
            size = "" if p.is_dir() else fmt(p.stat().st_size)
            rows.append(f'<tr><td><a href="{html.escape(name)}">{html.escape(name)}</a></td>'
                        f'<td class="n">{size}</td></tr>')
        (d / "index.html").write_text(f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(str(rel))}/</title>
<style>body{{font:14px/1.5 system-ui,sans-serif;max-width:800px;margin:24px auto;padding:0 16px;background:#fbfaf8;color:#1d1d1b}}
@media (prefers-color-scheme:dark){{body{{background:#141413;color:#ecebe6}}a{{color:#4cc79a}}}}
a{{color:#0b6e4f}}table{{border-collapse:collapse;width:100%}}td{{padding:4px 8px;border-bottom:1px solid #8883}}
.n{{text-align:right;font-variant-numeric:tabular-nums}}</style></head><body>
<p><a href="{'../' * (len(rel.parts))}">Hospital price file catalog</a></p>
<h1 style="font-size:20px">/{html.escape(str(rel))}/</h1><table>{''.join(rows)}</table></body></html>
""")


if __name__ == "__main__":
    write(Path(sys.argv[1]))
