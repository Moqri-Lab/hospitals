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


def session(retries: int = 3) -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept": "*/*"})
    # Be patient with rate limits: honour Retry-After and back off (2s, 4s, 8s).
    retry = Retry(total=retries, backoff_factor=2, status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=("GET", "HEAD"), respect_retry_after_header=True)
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
               timeout: float = 30.0, verify: bool | str = True) -> Fetched:
    """GET a URL but read at most ``max_bytes`` of the (decoded) body.

    Used only for small discovery files (cms-hpt.txt), never for price files.
    """
    try:
        with sess.get(url, stream=True, timeout=timeout, allow_redirects=True, verify=verify) as r:
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


_AIA_BUNDLES: dict[str, str] = {}


def aia_bundle(host: str) -> str | None:
    """CA bundle that adds the intermediate certificate a server forgot to send.

    Browsers fetch a missing intermediate from the certificate's "Authority
    Information Access" URL; Python does not. We do the same thing here, so
    the certificate is still fully verified against the normal CA store."""
    if host in _AIA_BUNDLES:
        return _AIA_BUNDLES[host]
    import ssl
    import tempfile

    import certifi
    from cryptography import x509
    from cryptography.hazmat.primitives.serialization import Encoding
    from cryptography.x509.oid import AuthorityInformationAccessOID, ExtensionOID

    bundle = None
    try:
        leaf = x509.load_pem_x509_certificate(ssl.get_server_certificate((host, 443), timeout=15).encode())
        aia = leaf.extensions.get_extension_for_oid(ExtensionOID.AUTHORITY_INFORMATION_ACCESS).value
        urls = [d.access_location.value for d in aia if d.access_method == AuthorityInformationAccessOID.CA_ISSUERS]
        pems = []
        for u in urls[:2]:
            raw = requests.get(u, timeout=15, headers={"User-Agent": USER_AGENT}).content
            try:
                cert = x509.load_der_x509_certificate(raw)
            except ValueError:
                cert = x509.load_pem_x509_certificate(raw)
            pems.append(cert.public_bytes(Encoding.PEM).decode())
        if pems:
            f = tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False)
            f.write(open(certifi.where()).read() + "\n" + "\n".join(pems))
            f.close()
            bundle = f.name
    except Exception:  # noqa: BLE001
        bundle = None
    _AIA_BUNDLES[host] = bundle
    return bundle


def fetch_text(sess: requests.Session, url: str, max_bytes: int = 2_000_000, timeout: float = 30.0) -> Fetched:
    """fetch_head, retrying once with the missing intermediate certificate on a TLS error."""
    f = fetch_head(sess, url, max_bytes=max_bytes, timeout=timeout)
    if f.error and "SSL" in f.error and "CERTIFICATE_VERIFY_FAILED" in f.error:
        from urllib.parse import urlparse
        bundle = aia_bundle(urlparse(url).hostname or "")
        if bundle:
            f = fetch_head(sess, url, max_bytes=max_bytes, timeout=timeout, verify=bundle)
    return f


def decode_text(body: bytes) -> str:
    # UTF-16 files (BOM, or NUL bytes between ASCII letters) are common from Windows tools
    if body[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return body.decode("utf-16", errors="replace")
    if len(body) > 8 and body[:200].count(b"\x00") > len(body[:200]) // 4:
        enc = "utf-16-le" if body[1:2] == b"\x00" else "utf-16-be"
        return body.decode(enc, errors="replace")
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return body.decode(enc)
        except UnicodeDecodeError:
            continue
    return body.decode("utf-8", errors="replace")


__all__ = ["session", "fetch_head", "decode_text", "Fetched"]
