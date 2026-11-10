"""Small HTTP helpers: a polite session, partial (head-of-file) downloads."""

from __future__ import annotations

from dataclasses import dataclass

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

USER_AGENT = (
    "hpt-hub/0.1 (open hospital price transparency research registry; "
    "+https://github.com/moqri/hpt-hub)"
)


def session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept": "*/*"})
    retry = Retry(total=2, backoff_factor=1.0, status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=("GET", "HEAD"))
    adapter = HTTPAdapter(max_retries=retry, pool_connections=32, pool_maxsize=32)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s


@dataclass
class Fetched:
    url: str
    final_url: str
    status: int | None
    content_type: str
    content_length: int | None
    last_modified: str
    body: bytes
    truncated: bool
    error: str = ""


def fetch_head(sess: requests.Session, url: str, max_bytes: int = 2_000_000,
               timeout: float = 30.0) -> Fetched:
    """GET a URL but read at most ``max_bytes`` of the (decoded) body.

    Used only for small discovery files (cms-hpt.txt), never for price files.
    """
    try:
        with sess.get(url, stream=True, timeout=timeout, allow_redirects=True) as r:
            chunks, n, truncated = [], 0, False
            for chunk in r.iter_content(chunk_size=65536):
                chunks.append(chunk)
                n += len(chunk)
                if n >= max_bytes:
                    truncated = True
                    break
            cl = r.headers.get("Content-Length")
            return Fetched(
                url=url,
                final_url=r.url,
                status=r.status_code,
                content_type=r.headers.get("Content-Type", ""),
                content_length=int(cl) if cl and cl.isdigit() else None,
                last_modified=r.headers.get("Last-Modified", ""),
                body=b"".join(chunks)[:max_bytes],
                truncated=truncated,
            )
    except requests.RequestException as e:
        return Fetched(url, url, None, "", None, "", b"", False, error=type(e).__name__ + ": " + str(e)[:200])


def decode_text(body: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return body.decode(enc)
        except UnicodeDecodeError:
            continue
    return body.decode("utf-8", errors="replace")


__all__ = ["session", "fetch_head", "decode_text", "Fetched"]
