"""Uploaded PDFs as a lesson source.

At upload: extract each page's text locally (pypdf), sort each page into a kind, and store the
text, the kinds and the PDF file itself in SQLite.
  text      - plain text: sent to Claude as extracted text (cheap)
  equations - many maths symbols or garbled short lines: extracted text would lose the maths
  figure    - has an embedded image (diagram, photo, scanned figure)
  scanned   - no text layer (a scan or handwritten notes)
Pages of the last three kinds are sent VISUALLY: as a 1-page PDF, so Claude reads the page's
text AND an image of it. Each such page is uploaded once to the Files API (its id is stored)
and reused in every later request.

Per lesson: pick the pages that best match the objective (simple keyword scoring on the
extracted text) and send only those pages, one cited document per page, so citations keep
page numbers. A scanned page has no text to score, so it gets its text neighbours' score; in a
PDF with no text at all, every page in your page range counts. At most PDF_MAX_PAGES pages,
of which at most VISUAL_PDF_MAX_PAGES visual ones.
"""
import io
import re

from pypdf import PdfReader, PdfWriter
from pypdf.errors import PdfReadError

import config
from db import store
from sources.base import LessonSource, SourceChunk

STOPWORDS = set("""
a an and are as at be by can do does for from has have how in into is it its of on or that the
their them then there these this to use using was were what when where which with without you your
minutes minute min hours hour able given least correctly between each per under
""".split())

VISUAL_KINDS = ("equations", "figure", "scanned")
MATH_CHARS = set("∑∫√≤≥±∞→∠ΩµμπθαβγδλστφωΔΣ∂≈≠×÷∥⊥′″∝") | {"="}


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


# ---------- upload ----------

def has_image(page) -> bool:
    """True if the page embeds an image (a figure, photo or scanned drawing)."""
    try:
        resources = page.get("/Resources")
        xobjects = resources.get_object().get("/XObject") if resources else None
        if not xobjects:
            return False
        return any(x.get_object().get("/Subtype") == "/Image" for x in xobjects.get_object().values())
    except Exception:  # odd or broken resources: treat as no image
        return False


def classify_page(text: str, image: bool) -> str:
    """text | equations | figure | scanned (see the module docstring)."""
    if len(text) < config.PDF_MIN_CHARS_PER_PAGE:
        return "scanned"
    if image:
        return "figure"
    symbols = sum(1 for ch in text if ch in MATH_CHARS)
    lines = [l for l in text.splitlines() if l.strip()]
    short = sum(1 for l in lines if len(l.strip()) <= 2)  # '+', 'R1', a lone digit of a fraction
    if symbols >= config.PDF_MATH_MIN_SYMBOLS or (len(lines) >= 10 and short / len(lines) >= config.PDF_SHORT_LINE_SHARE):
        return "equations"
    return "text"


def ingest_pdf(conn, profile_id: int, filename: str, data: bytes,
               page_from: int | None = None, page_to: int | None = None) -> tuple[int | None, str]:
    """Extract and store the PDF's text, page kinds and the file. Returns (upload_id, message).

    page_from/page_to (1-based, inclusive) limit which pages auto-pick searches later.
    The message says how the pages will be sent ('' if nothing worth saying).
    """
    try:
        reader = PdfReader(io.BytesIO(data))
        pages = [(p.extract_text() or "").strip() for p in reader.pages]
        images = [has_image(p) for p in reader.pages]
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

    kinds = [classify_page(t, img) for t, img in zip(pages, images)]
    lo, hi = (page_from or 1), (page_to or len(pages))
    in_range = kinds[lo - 1:hi]
    n_text = sum(1 for i in range(lo - 1, hi) if len(pages[i]) >= config.PDF_MIN_CHARS_PER_PAGE)
    upload_id = store.add_pdf(conn, profile_id, filename, pages, page_from, page_to, n_text,
                              data=data, kinds=kinds)

    n_visual = sum(1 for k in in_range if k in VISUAL_KINDS)
    n_scanned = in_range.count("scanned")
    msg = ""
    if n_visual:
        msg = (f"{filename}: {n_visual} of {len(in_range)} pages have equations, figures or no text, so they "
               f"are sent to Claude as images (at most {config.VISUAL_PDF_MAX_PAGES} per lesson; "
               f"about {config.VISUAL_PAGE_TOKENS:,} tokens each).")
    if n_text == 0:
        msg += (f" No page in {lo}-{hi} has a text layer, so pages can't be matched to a lesson by their "
                "words: the first pages of your page range are used. Set a tight page range.")
    elif n_scanned:
        msg += f" {n_scanned} scanned page(s) are picked when the text pages around them match a lesson."
    return upload_id, msg.strip()


def single_page_pdf(data: bytes, page_no: int) -> bytes:
    """Page `page_no` (1-based) of a PDF as its own 1-page PDF."""
    writer = PdfWriter()
    writer.add_page(PdfReader(io.BytesIO(data)).pages[page_no - 1])
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


# ---------- per lesson ----------

def page_scores(pages, words: set[str], has_text_layer: bool) -> list[int]:
    """Keyword score per page. Scanned pages take the average score of the nearest text page
    before and after them; in a PDF with no text layer every page scores 1 (page order wins)."""
    if not has_text_layer:
        return [1] * len(pages)
    texty = [len(p["text"]) >= config.PDF_MIN_CHARS_PER_PAGE for p in pages]
    own = [score_page(p["text"], words) if t else None for p, t in zip(pages, texty)]
    scores = []
    for i, s in enumerate(own):
        if s is not None:
            scores.append(s)
            continue
        before = next((own[j] for j in range(i - 1, -1, -1) if own[j] is not None), None)
        after = next((own[j] for j in range(i + 1, len(own)) if own[j] is not None), None)
        near = [x for x in (before, after) if x is not None]
        scores.append(sum(near) // len(near) if near else 0)
    return scores


class PdfSource(LessonSource):
    """All PDFs uploaded for one subject.

    `upload_file(filename, data) -> file_id` uploads a 1-page PDF to the Files API (LLM.upload_file).
    Without it (or for PDFs uploaded before Phase 5, whose file isn't stored), every page is sent
    as extracted text and scanned pages are skipped, as before."""

    kind = "pdf"

    def __init__(self, conn, profile_id: int, max_pages: int | None = None, upload_file=None):
        super().__init__()
        self.conn = conn
        self.profile_id = profile_id
        self.max_pages = max_pages or config.PDF_MAX_PAGES
        self.upload_file = upload_file

    def get_chunks(self, objective: str, profile: dict) -> list[SourceChunk]:
        words = keywords(objective)
        uploads = store.list_pdfs(self.conn, self.profile_id)
        scored = []  # (score, upload_order, page_no, upload, page, visual)
        for order, up in enumerate(uploads):
            pages = store.get_pdf_pages(self.conn, up["id"])
            can_send_visually = bool(up["has_data"]) and self.upload_file is not None
            for page, s in zip(pages, page_scores(pages, words, up["n_text_pages"] > 0)):
                visual = can_send_visually and (page["kind"] or "text") in VISUAL_KINDS
                if not visual and len(page["text"]) < config.PDF_MIN_CHARS_PER_PAGE:
                    continue  # scanned page that can't be sent as an image
                if s > 0:
                    scored.append((s, order, page["page_no"], up, page, visual))

        # Best scores first; ties go to the earlier file and page. Then apply the two caps.
        scored.sort(key=lambda x: (-x[0], x[1], x[2]))
        picked, n_visual = [], 0
        for s, order, page_no, up, page, visual in scored:
            if len(picked) >= self.max_pages:
                break
            if visual and n_visual >= config.VISUAL_PDF_MAX_PAGES:
                if len(page["text"]) < config.PDF_MIN_CHARS_PER_PAGE:
                    continue  # over the visual cap and no text to fall back on
                visual = False
            n_visual += visual
            picked.append((order, page_no, up, page, visual))
        picked.sort(key=lambda x: (x[0], x[1]))  # back to reading order

        chunks = []
        by_file: dict[str, dict] = {}
        for _, page_no, up, page, visual in picked:
            ref = f"{up['filename']} p.{page_no}"
            file_id = self._file_id(up, page) if visual else ""
            chunks.append(SourceChunk(text=page["text"], kind="pdf", ref=ref, title=ref, file_id=file_id))
            note = by_file.setdefault(up["filename"], {"pages": [], "visual": []})
            note["pages"].append(page_no)
            if visual:
                note["visual"].append(page_no)
        for filename, n in by_file.items():
            self.notes.append({"type": "pdf_pages", "file": filename, "pages": n["pages"],
                               "visual_pages": n["visual"]})
        if uploads and not picked:
            self.notes.append({"type": "pdf_no_match",
                               "reason": "no PDF page matched the objective's keywords"})
        return chunks

    def _file_id(self, up, page) -> str:
        """The page's Files API id, uploading the 1-page PDF the first time it is needed."""
        if page["file_id"]:
            return page["file_id"]
        data = single_page_pdf(store.get_pdf_data(self.conn, up["id"]), page["page_no"])
        file_id = self.upload_file(f"{up['filename']} p.{page['page_no']}.pdf", data)
        store.set_page_file_id(self.conn, up["id"], page["page_no"], file_id)
        return file_id
