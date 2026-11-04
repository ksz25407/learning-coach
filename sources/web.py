"""Web pages picked by the Curator, downloaded by this app (not by Claude).

- Each page's extracted text is cached in SQLite (web_pages), so a URL is downloaded once.
  Cached pages older than WEB_CACHE_MAX_DAYS are downloaded again.
- Downloads bigger than MAX_DOWNLOAD_BYTES are aborted ("too large").
- Text is capped at PAGE_MAX_TOKENS so one long page can't blow up the Teacher's cost.
- A failed or empty page is skipped and the Curator's next backup URL is tried instead.
- Videos are shown to the learner but never downloaded or taught from.
"""
import io
from datetime import datetime, timedelta
from urllib.parse import urlparse

import requests
import trafilatura
from pypdf import PdfReader

import config
from db import store
from sources.base import LessonSource, SourceChunk

MIN_TEXT_CHARS = 200  # less than this = empty page (usually a JavaScript-only site)
TEXT_KINDS = ("page", "pdf")  # resource kinds we can download and teach from


class FetchError(Exception):
    """A page could not be used. The message is the reason shown in the UI."""


def is_blocked(url: str) -> bool:
    """True if the URL is on a site listed in config.BLOCKED_DOMAINS."""
    parts = urlparse(url)
    host = (parts.hostname or "").lower()
    path = parts.path or "/"
    for entry in config.BLOCKED_DOMAINS:
        domain, _, prefix = entry.lower().partition("/")
        host_ok = host == domain or host.endswith("." + domain)
        if host_ok and (not prefix or path.lower().startswith("/" + prefix)):
            return True
    return False


def _is_fresh(fetched_at: str) -> bool:
    age = datetime.now() - datetime.fromisoformat(fetched_at)
    return age < timedelta(days=config.WEB_CACHE_MAX_DAYS)


def _download(url: str) -> tuple[bytes, str]:
    """Return (body, content_type). Raises FetchError with a short reason."""
    limit = config.MAX_DOWNLOAD_BYTES
    try:
        with requests.get(url, timeout=config.FETCH_TIMEOUT_S, stream=True,
                          headers={"User-Agent": config.FETCH_USER_AGENT}) as r:
            if r.status_code >= 400:
                raise FetchError(f"HTTP {r.status_code}")
            size = r.headers.get("Content-Length")
            if size and size.isdigit() and int(size) > limit:
                raise FetchError(f"too large ({int(size) / 1e6:.1f} MB, limit {limit / 1e6:.0f} MB)")
            body = bytearray()
            for part in r.iter_content(chunk_size=65536):
                body.extend(part)
                if len(body) > limit:  # no (or wrong) Content-Length: stop reading
                    raise FetchError(f"too large (over {limit / 1e6:.0f} MB)")
            return bytes(body), r.headers.get("Content-Type", "")
    except requests.Timeout:
        raise FetchError(f"timed out after {config.FETCH_TIMEOUT_S} s")
    except requests.RequestException as e:
        raise FetchError(f"download failed ({type(e).__name__})")


def _extract(url: str, body: bytes, content_type: str) -> str:
    if "pdf" in content_type.lower() or url.lower().split("?")[0].endswith(".pdf"):
        try:
            reader = PdfReader(io.BytesIO(body))
            return "\n\n".join(f"[Page {i}]\n{(p.extract_text() or '').strip()}"
                               for i, p in enumerate(reader.pages, start=1))
        except Exception:  # pypdf raises many kinds of errors on broken files
            raise FetchError("could not read the PDF")
    html = body.decode("utf-8", errors="replace")
    if content_type.lower().startswith("text/plain"):
        return html
    return trafilatura.extract(html, url=url) or ""


def fetch_text(conn, url: str) -> str:
    """Return the page's text (from cache if fresh). Raises FetchError with the reason."""
    cached = store.get_cached_page(conn, url)
    if cached is not None and _is_fresh(cached["fetched_at"]):
        return cached["text"]
    body, content_type = _download(url)
    text = _extract(url, body, content_type).strip()
    if len(text) < MIN_TEXT_CHARS:
        raise FetchError("page came back empty (it probably needs JavaScript)")
    text = text[: config.PAGE_MAX_TOKENS * 4]  # rough: 4 characters per token
    store.cache_page(conn, url, text)
    return text


class WebSource(LessonSource):
    """The Curator's resources for one milestone."""

    kind = "web"

    def __init__(self, conn, resources: list[dict]):
        super().__init__()
        self.conn = conn
        self.resources = resources

    def get_chunks(self, objective: str, profile: dict) -> list[SourceChunk]:
        primaries = [r for r in self.resources if r["role"] == "primary"]
        backups = [r for r in self.resources if r["role"] == "backup"]
        for r in self.resources:
            if r["kind"] not in TEXT_KINDS:
                self.notes.append({"type": "web_skipped", "url": r["url"],
                                   "reason": "video: shown to you, not used by the Teacher"})

        # Try text primaries first, then backups, until we have as many pages as there
        # were text primaries (at least 1). A failed page is replaced by the next one.
        candidates = [r for r in primaries + backups if r["kind"] in TEXT_KINDS]
        want = max(1, sum(1 for r in primaries if r["kind"] in TEXT_KINDS))
        chunks = []
        for r in candidates:
            if len(chunks) >= want:
                break
            if is_blocked(r["url"]):
                self.notes.append({"type": "web_skipped", "url": r["url"],
                                   "reason": "site blocks downloads (BLOCKED_DOMAINS in config.py), not tried"})
                continue
            try:
                text = fetch_text(self.conn, r["url"])
            except FetchError as e:
                self.notes.append({"type": "web_skipped", "url": r["url"], "reason": str(e)})
                continue
            chunks.append(SourceChunk(text=text, kind="web", ref=r["url"], title=r["title"]))
            self.notes.append({"type": "web_used", "url": r["url"], "title": r["title"],
                               "role": r["role"]})
        return chunks
