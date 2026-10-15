"""Storing, downloading and extracting text from manuals, guides and photos."""
import http.client
import ipaddress
import re
import socket
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path

from pypdf import PdfReader

from . import db

MAX_BYTES = 60 * 1024 * 1024
IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
TEXT_TYPES = {"text/plain", "text/markdown", "text/csv"}
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"


class DocumentError(Exception):
    pass


def docs_dir() -> Path:
    d = Path(db.DB_PATH).parent / "documents"
    d.mkdir(parents=True, exist_ok=True)
    return d


def safe_name(name: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.")
    return name[:80] or "document"


# ---------- downloading ----------

# NAT64 and 6to4 addresses embed an IPv4 address that may be private; Python counts the prefixes as global.
_EMBEDS_IPV4 = [ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("64:ff9b:1::/48"), ipaddress.ip_network("2002::/16")]


def is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%")[0])
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped:
            return ip.ipv4_mapped.is_global
        if any(ip in net for net in _EMBEDS_IPV4):
            return False
    return ip.is_global


def _check_public(url: str):
    """Only fetch from public internet hosts, so a link can't reach your router or other LAN devices.
    This gives a clear early error; the connection itself is checked again in _public_connection."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise DocumentError("Only http and https links can be downloaded.")
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))
    except (socket.gaierror, ValueError):
        raise DocumentError(f"Couldn't find the site {parts.hostname}.")
    if not all(is_public(info[4][0]) for info in infos):
        raise DocumentError(PRIVATE_ADDRESS)


PRIVATE_ADDRESS = "That link points to a private or local address, so it wasn't downloaded."


def _public_connection(address, *args, **kwargs):
    """Connect, then check the address actually connected to. A host can resolve differently a moment after
    _check_public looked it up (DNS rebinding), so this check is the one that counts."""
    sock = socket.create_connection(address, *args, **kwargs)
    if not is_public(sock.getpeername()[0]):
        sock.close()
        raise DocumentError(PRIVATE_ADDRESS)
    return sock


class _PublicHTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _public_connection


class _PublicHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _public_connection


class _PublicHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_PublicHTTPConnection, req)


class _PublicHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_PublicHTTPSConnection, req, context=self._context)


class _CheckedRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_public(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# No ProxyHandler: a proxy from HTTP(S)_PROXY would make every connection go to the proxy, bypassing the check.
_opener = urllib.request.OpenerDirector()
for _h in (urllib.request.UnknownHandler(), _PublicHTTPHandler(), _PublicHTTPSHandler(), urllib.request.HTTPDefaultErrorHandler(),
           _CheckedRedirects(), urllib.request.HTTPErrorProcessor()):
    _opener.add_handler(_h)


def download(url: str) -> tuple[bytes, str, str]:
    """Return (content, mime type, filename). Raises DocumentError with a readable message."""
    _check_public(url)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/pdf,text/html;q=0.9,*/*;q=0.5"})
    try:
        with _opener.open(req, timeout=30) as resp:
            data = resp.read(MAX_BYTES + 1)
            ctype = (resp.headers.get_content_type() or "").lower()
            final_url = resp.geturl()
    except urllib.error.HTTPError as e:
        raise DocumentError(f"The site returned an error ({e.code}) for that link.")
    except (urllib.error.URLError, TimeoutError) as e:
        raise DocumentError(f"Couldn't download that link: {getattr(e, 'reason', e)}")
    if len(data) > MAX_BYTES:
        raise DocumentError("That file is larger than 60 MB.")

    mime = sniff_mime(data, ctype)
    if mime is None:
        raise DocumentError(f"That link isn't a PDF, web page, image or text file (it was {ctype or 'unknown'}).")
    filename = Path(urllib.parse.urlsplit(final_url).path).name or "document"
    return data, mime, filename


def sniff_mime(data: bytes, declared: str = "") -> str | None:
    if data[:5] == b"%PDF-":
        return "application/pdf"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    head = data[:2048].lstrip().lower()
    if declared in ("text/html", "application/xhtml+xml") or head.startswith((b"<!doctype html", b"<html")):
        return "text/html"
    if declared in TEXT_TYPES:
        return "text/plain"
    try:
        data[:4096].decode("utf-8")
        return "text/plain" if declared in ("", "application/octet-stream") else None
    except UnicodeDecodeError:
        return None


# ---------- text extraction ----------

class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "header"}
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "table"}

    def __init__(self):
        super().__init__()
        self.parts, self.skip_depth, self.title = [], 0, ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip_depth:
            self.skip_depth -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self.skip_depth:
            self.parts.append(data)


def html_to_text(raw: bytes) -> tuple[str, str]:
    p = _TextExtractor()
    p.feed(raw.decode("utf-8", errors="replace"))
    text = re.sub(r"[ \t\r\f\v]+", " ", "".join(p.parts))
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    return text, p.title.strip()


def pdf_info(data: bytes) -> tuple[int, str]:
    """Page count and embedded text (empty for scanned PDFs)."""
    try:
        reader = PdfReader(BytesIO(data))
        pages = [(page.extract_text() or "") for page in reader.pages]
    except Exception as e:  # pypdf raises a variety of errors on damaged files
        raise DocumentError(f"Couldn't open that PDF: {e}")
    text = "\n\n".join(f"[Page {i}]\n{t.strip()}" for i, t in enumerate(pages, 1) if t.strip())
    return len(pages), text


# ---------- storage ----------

def save(conn, *, data: bytes, mime: str, filename: str, title: str, asset_id: int | None,
         kind: str, source: str, source_url: str | None = None) -> int:
    """Store the file on disk and register it. HTML is saved as extracted text."""
    pages = None
    if mime == "text/html":
        text, page_title = html_to_text(data)
        if len(text) < 200:
            raise DocumentError("That web page had almost no readable text. Try a direct link to the PDF manual instead.")
        data, mime, title = text.encode("utf-8"), "text/plain", title or page_title
        filename = Path(filename).stem + ".txt"
    elif mime == "application/pdf":
        pages, _ = pdf_info(data)

    cur = conn.execute(
        """INSERT INTO documents (asset_id, title, kind, source, source_url, filename, mime, size_bytes, pages)
           VALUES (?, ?, ?, ?, ?, '', ?, ?, ?)""",
        (asset_id, title or filename, kind, source, source_url, mime, len(data), pages),
    )
    doc_id = cur.lastrowid
    stored = f"{doc_id}-{safe_name(filename)}"
    (docs_dir() / stored).write_bytes(data)
    conn.execute("UPDATE documents SET filename = ? WHERE id = ?", (stored, doc_id))
    return doc_id


def path_for(doc: dict) -> Path:
    return docs_dir() / doc["filename"]


def delete_file(doc: dict):
    p = path_for(doc)
    if doc["filename"] and p.exists():
        p.unlink()
