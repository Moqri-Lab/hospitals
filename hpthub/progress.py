"""Crawl progress, printed to the log and, on GitHub Actions, shown as a live check run
("crawl progress") on the commit being crawled, so it can be followed while the crawl runs."""

from __future__ import annotations

import os
import time

import requests

_state = {"id": None, "last": 0.0, "t0": time.time()}


def _api(method: str, path: str, **body):
    tok, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not tok or not repo:
        return None
    try:
        r = requests.request(method, f"https://api.github.com/repos/{repo}{path}", json=body or None, timeout=15,
                             headers={"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"})
        return r.json() if r.ok else None
    except requests.RequestException:
        return None


def report(stage: str, done: int, total: int, force: bool = False) -> None:
    """Print progress; update the GitHub check run at most once a minute."""
    pct = 100 * done / total if total else 100.0
    mins = (time.time() - _state["t0"]) / 60
    line = f"{stage}: {done:,}/{total:,} ({pct:.0f}%) after {mins:.0f} min"
    print("  " + line, flush=True)
    if not force and time.time() - _state["last"] < 60:
        return
    _state["last"] = time.time()
    out = {"title": line, "summary": line}
    if _state["id"] is None:
        r = _api("POST", "/check-runs", name="crawl progress", head_sha=os.environ.get("GITHUB_SHA", ""),
                 status="in_progress", output=out)
        _state["id"] = r.get("id") if r else False
    elif _state["id"]:
        _api("PATCH", f"/check-runs/{_state['id']}", output=out)


def finish(summary: str) -> None:
    print(summary, flush=True)
    if _state["id"]:
        _api("PATCH", f"/check-runs/{_state['id']}", status="completed", conclusion="success",
             output={"title": summary, "summary": summary})
