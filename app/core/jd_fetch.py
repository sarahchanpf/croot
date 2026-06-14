"""JD ingestion: pasted URL or uploaded document -> plain text.

Ported verbatim from v1 (it was correct and well-tested). The URL fetcher is
SSRF-guarded — only http(s) URLs whose hostname, and every redirect hop's
hostname, resolves to a public IP; response size and time are capped. The
extracted text is fed to intake.run_turn as `jd_text`.
"""

from __future__ import annotations

import io
import ipaddress
import re
import socket
import sys
from html import unescape
from urllib.parse import urljoin, urlparse

import requests

from ..config import MAX_UPLOAD_BYTES

URL_LIKE_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)
_URL_FETCH_TIMEOUT = 10
_URL_FETCH_MAX_BYTES = 2_000_000
_URL_FETCH_MAX_REDIRECTS = 3
_URL_FETCH_MIN_TEXT = 500
_URL_FETCH_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


def extract_pdf_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        parts = []
        for page in reader.pages:
            try:
                parts.append(page.extract_text() or "")
            except Exception:
                continue
        return "\n".join(parts)
    except Exception as exc:
        print(f"croot.jd pdf_error={exc}", file=sys.stderr, flush=True)
        return ""


def extract_docx_text(data: bytes) -> str:
    try:
        from docx import Document
        doc = Document(io.BytesIO(data))
        return "\n".join(p.text for p in doc.paragraphs if p.text)
    except Exception as exc:
        print(f"croot.jd docx_error={exc}", file=sys.stderr, flush=True)
        return ""


def _is_public_host(hostname: str) -> bool:
    """Resolve hostname; confirm every address is a public routable IP. Blocks
    SSRF into metadata, link-local, loopback, private, and reserved ranges."""
    try:
        infos = socket.getaddrinfo(hostname, None)
    except OSError:
        return False
    if not infos:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return False
    return True


def strip_html(html_text: str) -> str:
    html_text = re.sub(
        r"<(script|style|noscript|svg|template|head)\b[^>]*>.*?</\1>",
        " ", html_text, flags=re.IGNORECASE | re.DOTALL,
    )
    html_text = re.sub(
        r"</?(?:p|div|li|ul|ol|br|h[1-6]|tr|td|section|article|header|"
        r"footer|main|nav|aside|figure|figcaption)\b[^>]*>",
        "\n", html_text, flags=re.IGNORECASE,
    )
    html_text = re.sub(r"<[^>]+>", "", html_text)
    html_text = unescape(html_text)
    html_text = re.sub(r"[ \t]+\n", "\n", html_text)
    html_text = re.sub(r"\n{3,}", "\n\n", html_text)
    return html_text.strip()


# JS-rendered job boards whose raw HTML is a SPA shell (the JD body loads via
# JS, so strip_html only sees page chrome / the job-list nav). Each exposes a
# public JSON API with the actual posting — route to that instead.
_GREENHOUSE_HOSTS = {"boards.greenhouse.io", "job-boards.greenhouse.io"}
_LEVER_HOSTS = {"jobs.lever.co"}


def _board_api(url: str):
    """Map a known JS-rendered board URL to its JSON API. Returns (api_url, kind)
    or (None, None)."""
    p = urlparse(url)
    host = (p.hostname or "").lower()
    parts = [seg for seg in p.path.split("/") if seg]
    if host in _GREENHOUSE_HOSTS and len(parts) >= 3 and parts[-2] == "jobs":
        return f"https://boards-api.greenhouse.io/v1/boards/{parts[-3]}/jobs/{parts[-1]}", "greenhouse"
    if host in _LEVER_HOSTS and len(parts) >= 2:
        return f"https://api.lever.co/v0/postings/{parts[0]}/{parts[1]}", "lever"
    return None, None


def _fetch_board_api(api_url: str, kind: str) -> str:
    """Pull a posting from a board's JSON API and return clean text ('' on miss)."""
    p = urlparse(api_url)
    if not _is_public_host(p.hostname or ""):
        return ""
    try:
        resp = requests.get(api_url, timeout=_URL_FETCH_TIMEOUT,
                            headers={"User-Agent": _URL_FETCH_UA, "Accept": "application/json"})
        if resp.status_code >= 400:
            return ""
        data = resp.json()
    except (requests.RequestException, ValueError):
        return ""
    if kind == "greenhouse":
        loc = (data.get("location") or {}).get("name") or ""
        raw = "\n".join([data.get("title") or "",
                         f"Location: {loc}" if loc else "",
                         unescape(data.get("content") or "")])
        return strip_html(raw)
    if kind == "lever":
        cats = data.get("categories") or {}
        parts = [data.get("text") or ""]
        if cats.get("location"):
            parts.append("Location: " + cats["location"])
        parts.append(unescape(data.get("descriptionPlain") or data.get("description") or ""))
        for lst in (data.get("lists") or []):
            parts.append(lst.get("text") or "")
            parts.append(unescape(lst.get("content") or ""))
        parts.append(unescape(data.get("additionalPlain") or data.get("additional") or ""))
        return strip_html("\n".join(parts))
    return ""


def fetch_from_url(url: str) -> str:
    """Fetch a job-posting URL and return its body as plain text. Raises
    ValueError with a recruiter-friendly message on any failure."""
    # Known JS-rendered boards (Greenhouse/Lever) -> their JSON API, which has
    # the real posting. Fall back to the raw fetch below if the API misses.
    api_url, kind = _board_api(url.strip())
    if api_url:
        text = _fetch_board_api(api_url, kind)
        if len(text) >= _URL_FETCH_MIN_TEXT:
            return text

    current = url.strip()
    for _ in range(_URL_FETCH_MAX_REDIRECTS + 1):
        parsed = urlparse(current)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("Only public http(s) URLs are supported.")
        if not _is_public_host(parsed.hostname):
            raise ValueError("URL host isn't reachable from this service.")
        try:
            resp = requests.get(
                current, timeout=_URL_FETCH_TIMEOUT, allow_redirects=False,
                headers={
                    "User-Agent": _URL_FETCH_UA,
                    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                },
                stream=True,
            )
        except requests.RequestException as exc:
            raise ValueError(
                f"Couldn't fetch URL ({exc.__class__.__name__}). "
                "Try pasting the JD text instead."
            )
        if resp.is_redirect or resp.is_permanent_redirect:
            location = resp.headers.get("Location") or ""
            resp.close()
            if not location:
                raise ValueError("URL redirect was missing a Location header.")
            current = urljoin(current, location)
            continue
        if resp.status_code >= 400:
            resp.close()
            raise ValueError(
                f"URL returned HTTP {resp.status_code} — likely a login wall "
                "or anti-bot page. Try pasting the JD text instead."
            )
        chunks: list[bytes] = []
        total = 0
        for chunk in resp.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > _URL_FETCH_MAX_BYTES:
                resp.close()
                raise ValueError("URL response too large (max 2 MB).")
            chunks.append(chunk)
        resp.close()
        body = b"".join(chunks)
        encoding = resp.encoding or resp.apparent_encoding or "utf-8"
        try:
            html_text = body.decode(encoding, errors="ignore")
        except (LookupError, TypeError):
            html_text = body.decode("utf-8", errors="ignore")
        text = strip_html(html_text)
        if len(text) < _URL_FETCH_MIN_TEXT:
            raise ValueError(
                "URL response had too little readable text — likely a login "
                "wall or anti-bot page. Try pasting the JD text instead."
            )
        return text
    raise ValueError("Too many redirects.")


def read_uploaded_text(file_storage) -> tuple[str, str]:
    """Return (extracted_text, source_label) for a Werkzeug FileStorage."""
    name = (file_storage.filename or "").lower()
    data = file_storage.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError("File too large (max 4 MB).")
    if name.endswith(".pdf"):
        return extract_pdf_text(data), "pdf"
    if name.endswith(".docx"):
        return extract_docx_text(data), "docx"
    if name.endswith(".doc"):
        raise ValueError("Legacy .doc isn't supported — save as .docx or paste the text.")
    try:
        return data.decode("utf-8", errors="ignore"), "text"
    except Exception:
        return data.decode("latin-1", errors="ignore"), "text"
