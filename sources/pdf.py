"""Uploaded PDFs as a lesson source.

At upload: extract each page's text locally (pypdf) and store it in SQLite.
Per lesson: pick the pages that best match the objective (simple keyword scoring) and send
only those pages to the Teacher, one cited document per page, so citations keep page numbers.
Scanned PDFs (no text layer) can't be used; the upload warns about that.
"""
import io
import re

from pypdf import PdfReader
from pypdf.errors import PdfReadError

import config
from db import store
from sources.base import LessonSource, SourceChunk

STOPWORDS = set("""
a an and are as at be by can do does for from has have how in into is it its of on or that the
their them then there these this to use using was were what when where which with without you your
minutes minute min hours hour able given least correctly between each per under
""".split())


def keywords(text: str) -> set[str]:
    """Lowercase words of 3+ letters, minus common filler words."""
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) >= 3 and w not in STOPWORDS}


def score_page(page_text: str, words: set[str]) -> int:
    """How often the keywords appear on the page. Each word counts at most 5 times,
    so one very common word can't win on its own."""
    counts = {}
    for w in re.findall(r"[a-z0-9]+", page_text.lower()):
        if w in words:
            counts[w] = counts.get(w, 0) + 1
    return sum(min(c, 5) for c in counts.values())


def ingest_pdf(conn, profile_id: int, filename: str, data: bytes,
               page_from: int | None = None, page_to: int | None = None) -> tuple[int | None, str]:
    """Extract and store the PDF's text. Returns (upload_id, warning).

    upload_id is None if the PDF has no usable text at all (then nothing is stored).
    page_from/page_to (1-based, inclusive) limit which pages auto-pick searches later.
    """
    try:
        reader = PdfReader(io.BytesIO(data))
        pages = [(p.extract_text() or "").strip() for p in reader.pages]
    except (PdfReadError, ValueError, OSError) as e:
        raise ValueError(f"Could not read {filename}: {e}") from e
    if not pages:
        raise ValueError(f"{filename} has no pages.")

    page_from = page_from or None
    page_to = page_to or None
    if page_from and page_to and page_from > page_to:
        raise ValueError(f"Page range {page_from}-{page_to} is backwards.")
    if page_from and page_from > len(pages):
        raise ValueError(f"{filename} only has {len(pages)} pages.")
    if page_to:
        page_to = min(page_to, len(pages))

    lo, hi = (page_from or 1), (page_to or len(pages))
    in_range = pages[lo - 1:hi]
    n_text = sum(1 for t in in_range if len(t) >= config.PDF_MIN_CHARS_PER_PAGE)

    if n_text == 0:
        return None, (f"{filename}: no text layer found in pages {lo}-{hi}. It looks like a scanned PDF, "
                      "so it can't be used as a source (it was not saved).")
    upload_id = store.add_pdf(conn, profile_id, filename, pages, page_from, page_to, n_text)
    warning = ""
    if n_text < len(in_range) / 2:
        warning = (f"{filename}: only {n_text} of {len(in_range)} pages have readable text. "
                   "The rest look scanned and will be ignored.")
    return upload_id, warning


class PdfSource(LessonSource):
    """All PDFs uploaded for one subject."""

    kind = "pdf"

    def __init__(self, conn, profile_id: int, max_pages: int | None = None):
        super().__init__()
        self.conn = conn
        self.profile_id = profile_id
        self.max_pages = max_pages or config.PDF_MAX_PAGES

    def get_chunks(self, objective: str, profile: dict) -> list[SourceChunk]:
        words = keywords(objective)
        scored = []  # (score, upload_order, page_no, filename, text)
        for order, up in enumerate(store.list_pdfs(self.conn, self.profile_id)):
            for page in store.get_pdf_pages(self.conn, up["id"]):
                if len(page["text"]) < config.PDF_MIN_CHARS_PER_PAGE:
                    continue  # scanned / empty page
                s = score_page(page["text"], words)
                if s > 0:
                    scored.append((s, order, page["page_no"], up["filename"], page["text"]))

        # Best scores first; ties go to the earlier file and page.
        scored.sort(key=lambda x: (-x[0], x[1], x[2]))
        picked = sorted(scored[: self.max_pages], key=lambda x: (x[1], x[2]))  # back to reading order

        chunks = []
        by_file: dict[str, list[int]] = {}
        for _, _, page_no, filename, text in picked:
            ref = f"{filename} p.{page_no}"
            chunks.append(SourceChunk(text=text, kind="pdf", ref=ref, title=ref))
            by_file.setdefault(filename, []).append(page_no)
        for filename, page_nos in by_file.items():
            self.notes.append({"type": "pdf_pages", "file": filename, "pages": page_nos})
        if store.list_pdfs(self.conn, self.profile_id) and not picked:
            self.notes.append({"type": "pdf_no_match",
                               "reason": "no PDF page matched the objective's keywords"})
        return chunks
