"""Lesson sources: priority order, PDF page picking, web downloads and the download cache."""
from datetime import datetime, timedelta

import pytest

import config
from db import store
from sources import web
from sources.base import ModelKnowledgeSource, SourceChunk, merge_sources
from sources.pdf import PdfSource, ingest_pdf, keywords
from sources.web import FetchError, WebSource, fetch_text
from tests.helpers import FakeHTTP, FakeResponse, Timeout, article_html, make_pdf

FILLER = " Some more filler words about the general software interface and settings." * 3
WALLS = "Walls: draw a wall with the Wall tool, set wall type, height and location line." + FILLER
ROOF = "Roofs: create a roof by footprint, set slope and overhang for the roof edges." + FILLER


@pytest.fixture
def pid(conn):
    return store.create_profile(conn)


@pytest.fixture
def http(monkeypatch):
    fake = FakeHTTP({})
    monkeypatch.setattr(web.requests, "get", fake.get)
    return fake


# ---------- base ----------

def test_merge_sources_priority():
    chunks = [SourceChunk("a", "model", "m"), SourceChunk("b", "web", "w"),
              SourceChunk("c", "pdf", "p"), SourceChunk("d", "web", "w2")]
    assert [c.text for c in merge_sources(chunks)] == ["c", "b", "d", "a"]


def test_model_knowledge_source_is_empty():
    assert ModelKnowledgeSource().get_chunks("obj", {}) == []


# ---------- PDF ----------

def test_keywords_drop_filler_words():
    assert keywords("Model the walls in 20 minutes") == {"model", "walls"}


def test_pdf_picks_matching_pages_in_page_order(conn, pid):
    ingest_pdf(conn, pid, "manual.pdf", make_pdf([ROOF, WALLS, ROOF, WALLS]))
    src = PdfSource(conn, pid)
    chunks = src.get_chunks("Walls: draw the ground-floor wall types", {})
    assert [c.ref for c in chunks] == ["manual.pdf p.2", "manual.pdf p.4"]
    assert all(c.kind == "pdf" for c in chunks)
    assert src.notes == [{"type": "pdf_pages", "file": "manual.pdf", "pages": [2, 4]}]


def test_pdf_page_cap(conn, pid):
    ingest_pdf(conn, pid, "manual.pdf", make_pdf([WALLS] * 6))
    chunks = PdfSource(conn, pid, max_pages=3).get_chunks("wall", {})
    assert len(chunks) == 3


def test_pdf_page_range_limits_search(conn, pid):
    ingest_pdf(conn, pid, "manual.pdf", make_pdf([WALLS, WALLS, WALLS, WALLS]), page_from=3, page_to=4)
    chunks = PdfSource(conn, pid).get_chunks("wall", {})
    assert [c.ref for c in chunks] == ["manual.pdf p.3", "manual.pdf p.4"]


def test_pdf_no_match_is_noted(conn, pid):
    ingest_pdf(conn, pid, "manual.pdf", make_pdf([ROOF]))
    src = PdfSource(conn, pid)
    assert src.get_chunks("stairs railings", {}) == []
    assert src.notes[0]["type"] == "pdf_no_match"


def test_scanned_pdf_is_not_saved(conn, pid):
    upload_id, warning = ingest_pdf(conn, pid, "scan.pdf", make_pdf(["", "", ""]))
    assert upload_id is None
    assert "scanned" in warning
    assert store.list_pdfs(conn, pid) == []


def test_mostly_scanned_pdf_warns(conn, pid):
    upload_id, warning = ingest_pdf(conn, pid, "mixed.pdf", make_pdf([WALLS, "", "", ""]))
    assert upload_id is not None
    assert "only 1 of 4 pages" in warning


def test_backwards_page_range_rejected(conn, pid):
    with pytest.raises(ValueError):
        ingest_pdf(conn, pid, "manual.pdf", make_pdf([WALLS, WALLS]), page_from=2, page_to=1)


def test_unreadable_pdf_raises_value_error(conn, pid):
    with pytest.raises(ValueError):
        ingest_pdf(conn, pid, "broken.pdf", b"not a pdf at all")


# ---------- web downloads ----------

URL = "https://example.com/walls"


def test_download_extracts_and_caches(conn, http):
    http.pages[URL] = FakeResponse(article_html("Revit walls"))
    text = fetch_text(conn, URL)
    assert "Revit walls" in text
    assert fetch_text(conn, URL) == text
    assert http.calls == [URL]  # second call came from the cache


def test_fresh_cache_is_not_downloaded_again(conn, http):
    store.cache_page(conn, URL, "cached text")
    assert fetch_text(conn, URL) == "cached text"
    assert http.calls == []


def test_stale_cache_is_downloaded_again(conn, http):
    store.cache_page(conn, URL, "old text")
    old = (datetime.now() - timedelta(days=config.WEB_CACHE_MAX_DAYS + 1)).isoformat(timespec="seconds")
    conn.execute("UPDATE web_pages SET fetched_at = ? WHERE url = ?", (old, URL))
    http.pages[URL] = FakeResponse(article_html("New walls"))

    text = fetch_text(conn, URL)

    assert "New walls" in text
    assert http.calls == [URL]
    row = store.get_cached_page(conn, URL)
    assert "New walls" in row["text"]
    assert row["fetched_at"] > old  # cache row updated


def test_too_large_by_content_length_is_not_read(conn, http):
    resp = FakeResponse(article_html("x"), headers={"Content-Length": str(config.MAX_DOWNLOAD_BYTES + 1)})
    http.pages[URL] = resp
    with pytest.raises(FetchError, match="too large"):
        fetch_text(conn, URL)
    assert resp.bytes_read == 0  # aborted before downloading the body
    assert store.get_cached_page(conn, URL) is None


def test_too_large_streamed_body_is_aborted(conn, http, monkeypatch):
    monkeypatch.setattr(config, "MAX_DOWNLOAD_BYTES", 1000)
    resp = FakeResponse(b"a" * 50_000, chunk=500)  # no Content-Length header
    http.pages[URL] = resp
    with pytest.raises(FetchError, match="too large"):
        fetch_text(conn, URL)
    assert resp.bytes_read <= 1500  # stopped soon after passing the limit, not all 50 KB


def test_text_is_capped(conn, http, monkeypatch):
    monkeypatch.setattr(config, "PAGE_MAX_TOKENS", 100)
    http.pages[URL] = FakeResponse(article_html("Long page", repeat=200))
    assert len(fetch_text(conn, URL)) == 400


def test_http_error_reason(conn, http):
    with pytest.raises(FetchError, match="HTTP 404"):
        fetch_text(conn, URL)


def test_timeout_reason(conn, http):
    http.pages[URL] = Timeout()
    with pytest.raises(FetchError, match="timed out"):
        fetch_text(conn, URL)


def test_empty_javascript_page(conn, http):
    http.pages[URL] = FakeResponse(b"<html><body><div id='app'></div><script>x()</script></body></html>")
    with pytest.raises(FetchError, match="empty"):
        fetch_text(conn, URL)


def test_web_pdf_link_is_read(conn, http):
    http.pages["https://example.com/guide.pdf"] = FakeResponse(
        make_pdf([WALLS, ROOF]), headers={"Content-Type": "application/pdf"})
    text = fetch_text(conn, "https://example.com/guide.pdf")
    assert "[Page 1]" in text and "Wall tool" in text


# ---------- WebSource ----------

def res(url, role="primary", kind="page"):
    return {"url": url, "title": url.rsplit("/", 1)[-1], "kind": kind, "role": role}


def test_failed_primary_replaced_by_backup(conn, http):
    http.pages["https://a.com/backup"] = FakeResponse(article_html("Backup page"))
    src = WebSource(conn, [res("https://a.com/broken"), res("https://a.com/backup", role="backup")])
    chunks = src.get_chunks("walls", {})
    assert [c.ref for c in chunks] == ["https://a.com/backup"]
    assert {"type": "web_skipped", "url": "https://a.com/broken", "reason": "HTTP 404"} in src.notes
    assert any(n["type"] == "web_used" and n["role"] == "backup" for n in src.notes)


def test_backup_not_downloaded_when_primary_works(conn, http):
    http.pages["https://a.com/p"] = FakeResponse(article_html("Primary"))
    src = WebSource(conn, [res("https://a.com/p"), res("https://a.com/b", role="backup")])
    assert len(src.get_chunks("walls", {})) == 1
    assert http.calls == ["https://a.com/p"]


def test_video_is_not_downloaded(conn, http):
    http.pages["https://a.com/p"] = FakeResponse(article_html("Primary"))
    src = WebSource(conn, [res("https://youtube.com/v", kind="video"), res("https://a.com/p")])
    chunks = src.get_chunks("walls", {})
    assert [c.ref for c in chunks] == ["https://a.com/p"]
    assert "https://youtube.com/v" not in http.calls
    assert any(n["url"] == "https://youtube.com/v" and "video" in n["reason"] for n in src.notes)


def test_all_pages_fail_gives_no_chunks(conn, http):
    src = WebSource(conn, [res("https://a.com/1"), res("https://a.com/2", role="backup")])
    assert src.get_chunks("walls", {}) == []
    assert len([n for n in src.notes if n["type"] == "web_skipped"]) == 2
