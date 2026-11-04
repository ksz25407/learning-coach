"""Phase 5 step 3: course-PDF pages with equations, figures or no text are sent visually
(as 1-page PDFs via the Files API), cached, capped and counted in cost estimates. No real API calls."""
import io
import sqlite3
from types import SimpleNamespace

import pytest
from pypdf import PdfReader

import config
from agents import fact_checker
from agents.teacher import write_lesson
from db import store
from llm import LLM, LLMError
from orchestrator import Coach
from sources.base import SourceChunk
from sources.pdf import PdfSource, classify_page, ingest_pdf, single_page_pdf
from tests.conftest import pass_all_checks
from tests.helpers import make_pdf
from tests.test_teacher import MILESTONE, PROFILE

FILLER = " This page explains the method step by step in plain words for the course." * 3
TEXT = "Thevenin equivalent: replace the network by a source and a resistance." + FILLER
MATH = "Thevenin equivalent:\nV_th = V_oc\nR_th = V_oc / I_sc\nI_L = V_th / (R_th + R_L)" + FILLER
OTHER = "Diode clamping circuits shift the waveform by a DC level set by the capacitor." + FILLER


class FakeFiles:
    """Stands in for client.files: records uploads and deletes."""

    def __init__(self, fail_delete=False):
        self.uploaded, self.deleted, self.fail_delete = [], [], fail_delete

    def upload(self, file):
        name, data, media_type = file
        self.uploaded.append((name, data, media_type))
        return SimpleNamespace(id=f"file_{len(self.uploaded)}")

    def delete(self, file_id):
        if self.fail_delete:
            raise RuntimeError("not found")
        self.deleted.append(file_id)


@pytest.fixture
def files(fake):
    fake.files = FakeFiles()
    return fake.files


@pytest.fixture
def pid(conn):
    return store.create_profile(conn)


# ---------- sorting pages into kinds ----------

def test_classify_page():
    assert classify_page(TEXT, image=False) == "text"
    assert classify_page(MATH, image=False) == "equations"  # several '=' signs
    assert classify_page("Ω and µ and ≤ appear here." + FILLER, image=False) == "equations"
    garbled = "\n".join(["Some words on a line here"] * 7 + ["R", "+", "2", "−"]) + FILLER
    assert classify_page(garbled, image=False) == "equations"  # many 1-2 character lines
    assert classify_page(TEXT, image=True) == "figure"
    assert classify_page("", image=False) == "scanned"


def test_upload_stores_file_and_page_kinds(conn, pid):
    upload_id, msg = ingest_pdf(conn, pid, "notes.pdf", make_pdf([TEXT, MATH, OTHER, ""], image_pages=(3,)))
    kinds = [p["kind"] for p in store.get_pdf_pages(conn, upload_id)]
    assert kinds == ["text", "equations", "figure", "scanned"]
    assert store.get_pdf_data(conn, upload_id).startswith(b"%PDF")
    assert "3 of 4 pages" in msg and "as images" in msg


def test_single_page_pdf_is_that_page():
    data = make_pdf([TEXT, MATH, OTHER])
    page = PdfReader(io.BytesIO(single_page_pdf(data, 2)))
    assert len(page.pages) == 1 and "V_th = V_oc" in page.pages[0].extract_text()


# ---------- picking pages ----------

def test_visual_page_is_uploaded_once_and_reused(conn, pid, llm, files):
    ingest_pdf(conn, pid, "notes.pdf", make_pdf([TEXT, MATH]))
    src = PdfSource(conn, pid, upload_file=llm.upload_file)
    chunks = src.get_chunks("Thevenin equivalent", {})

    assert [(c.ref, c.file_id) for c in chunks] == [("notes.pdf p.1", ""), ("notes.pdf p.2", "file_1")]
    name, data, media_type = files.uploaded[0]
    assert name == "notes.pdf p.2.pdf" and media_type == "application/pdf"
    assert len(PdfReader(io.BytesIO(data)).pages) == 1
    assert src.notes == [{"type": "pdf_pages", "file": "notes.pdf", "pages": [1, 2], "visual_pages": [2]}]

    again = PdfSource(conn, pid, upload_file=llm.upload_file).get_chunks("Thevenin equivalent", {})
    assert again[1].file_id == "file_1" and len(files.uploaded) == 1  # not uploaded twice


def test_visual_cap_sends_the_rest_as_text(conn, pid, llm, files, monkeypatch):
    monkeypatch.setattr(config, "VISUAL_PDF_MAX_PAGES", 2)
    ingest_pdf(conn, pid, "notes.pdf", make_pdf([MATH] * 4 + [""]))
    chunks = PdfSource(conn, pid, upload_file=llm.upload_file).get_chunks("Thevenin equivalent", {})
    visual = [c.ref for c in chunks if c.file_id]
    assert visual == ["notes.pdf p.1", "notes.pdf p.2"]  # the best two (ties: page order)
    # p.3 and p.4 have text, so they are sent as text; the scanned p.5 has nothing to fall back on
    assert [c.ref for c in chunks if not c.file_id] == ["notes.pdf p.3", "notes.pdf p.4"]


def test_scanned_page_scores_like_its_neighbours(conn, pid, llm, files):
    ingest_pdf(conn, pid, "notes.pdf", make_pdf([OTHER, TEXT, "", TEXT, OTHER, "", OTHER]))
    chunks = PdfSource(conn, pid, upload_file=llm.upload_file).get_chunks("Thevenin equivalent", {})
    assert [c.ref for c in chunks] == ["notes.pdf p.2", "notes.pdf p.3", "notes.pdf p.4"]
    assert chunks[1].file_id  # the scan between two Thevenin pages, sent as an image


def test_fully_scanned_pdf_uses_page_order(conn, pid, llm, files, monkeypatch):
    monkeypatch.setattr(config, "VISUAL_PDF_MAX_PAGES", 3)
    ingest_pdf(conn, pid, "scan.pdf", make_pdf([""] * 6), page_from=2, page_to=6)
    chunks = PdfSource(conn, pid, upload_file=llm.upload_file).get_chunks("anything", {})
    assert [c.ref for c in chunks] == ["scan.pdf p.2", "scan.pdf p.3", "scan.pdf p.4"]


def test_without_uploader_everything_is_text(conn, pid):
    ingest_pdf(conn, pid, "notes.pdf", make_pdf([TEXT, MATH, ""]))
    chunks = PdfSource(conn, pid).get_chunks("Thevenin equivalent", {})
    assert [(c.ref, c.file_id) for c in chunks] == [("notes.pdf p.1", ""), ("notes.pdf p.2", "")]


def test_pdf_uploaded_before_phase5_is_text_only(conn, pid, llm, files):
    store.add_pdf(conn, pid, "old.pdf", [TEXT, MATH], None, None, 2)  # no file stored
    chunks = PdfSource(conn, pid, upload_file=llm.upload_file).get_chunks("Thevenin equivalent", {})
    assert len(chunks) == 2 and not any(c.file_id for c in chunks) and files.uploaded == []


# ---------- what the agents receive ----------

VISUAL = SourceChunk(MATH, "pdf", "notes.pdf p.2", "notes.pdf p.2", file_id="file_9")
WEB = SourceChunk("Web text", "web", "https://e.com", "Guide")


def test_teacher_sends_visual_page_as_file_with_citations_and_caches_documents(llm, fake):
    fake.messages.text_replies = ["## A\nHi", "## A\nHi again"]
    write_lesson(llm, PROFILE, MILESTONE, chunks=[VISUAL, WEB])
    content = fake.messages.calls[0][1]["messages"][0]["content"]
    assert content[0]["source"] == {"type": "file", "file_id": "file_9"}
    assert content[0]["citations"] == {"enabled": True}
    assert content[1]["source"]["type"] == "text"
    assert content[2]["text"].startswith("Milestone:") and content[2]["cache_control"] == {"type": "ephemeral"}
    assert len(content) == 3

    write_lesson(llm, PROFILE, MILESTONE, chunks=[VISUAL, WEB], previous="old", feedback="fix it")
    revision = fake.messages.calls[1][1]["messages"][0]["content"]
    assert revision[:3] == content  # same cached prefix in every round
    assert "PREVIOUS DRAFT" in revision[3]["text"] and "cache_control" not in revision[3]


def test_fact_checker_gets_visual_page_without_citations_and_caches_it():
    content = fact_checker.build_messages("LESSON TEXT", [VISUAL, WEB])[0]["content"]
    assert content[0]["source"] == {"type": "file", "file_id": "file_9"} and "citations" not in content[0]
    assert content[2]["cache_control"] == {"type": "ephemeral"}
    assert content[3]["text"].endswith("LESSON TEXT")
    no_docs = fact_checker.build_messages("L", [])[0]["content"]
    assert len(no_docs) == 1 and "cache_control" not in no_docs[0]


# ---------- LLM wrapper ----------

def test_size_check_leaves_files_out_of_count_tokens(llm, fake, monkeypatch):
    monkeypatch.setattr(config, "TOKEN_COUNT_THRESHOLD", 10_000)
    counted = []
    fake.messages.count_tokens = lambda **kw: (counted.append(kw), SimpleNamespace(input_tokens=500))[1]
    docs = [{"type": "document", "source": {"type": "file", "file_id": f"f{i}"}} for i in range(3)]
    messages = [{"role": "user", "content": docs + [{"type": "text", "text": "hi"}]}]
    llm._check_size({"model": "m", "system": "s", "messages": messages})  # 3 x 4,500 > 10,000
    sent = counted[0]["messages"][0]["content"]
    assert sent == [{"type": "text", "text": "hi"}]  # files removed: count_tokens can't read them

    monkeypatch.setattr(config, "MAX_INPUT_TOKENS", 10_000)  # 500 counted + 13,500 estimated
    with pytest.raises(LLMError, match="about 14000 tokens"):
        llm._check_size({"model": "m", "system": "s", "messages": messages})


def test_small_prompt_with_one_file_skips_counting(llm, fake):
    fake.messages.count_tokens = lambda **kw: pytest.fail("should not count")
    doc = {"type": "document", "source": {"type": "file", "file_id": "f"}}
    llm._check_size({"model": "m", "system": "s", "messages": [{"role": "user", "content": [doc]}]})


def test_upload_error_is_an_llm_error(conn):
    def broken(file):
        raise RuntimeError("network down")
    llm = LLM(conn, client=SimpleNamespace(files=SimpleNamespace(upload=broken)))
    with pytest.raises(LLMError, match="Couldn't upload notes.pdf"):
        llm.upload_file("notes.pdf", b"%PDF")


def test_delete_file_never_raises(conn):
    llm = LLM(conn, client=SimpleNamespace(files=FakeFiles(fail_delete=True)))
    assert llm.delete_file("file_1") is False


# ---------- orchestrator: deletes and cost ----------

@pytest.fixture
def coach(conn, llm, fake, files):
    pass_all_checks(fake)
    return Coach(conn, llm)


def test_deleting_a_pdf_or_subject_deletes_its_uploaded_pages(coach, conn, files):
    pid = store.create_profile(conn)
    a, _ = ingest_pdf(conn, pid, "a.pdf", make_pdf([MATH]))
    b, _ = ingest_pdf(conn, pid, "b.pdf", make_pdf([MATH]))
    coach._pdf_source(pid).get_chunks("Thevenin equivalent", {})  # uploads both pages
    assert store.pdf_file_ids(conn, upload_id=a) == ["file_1"]

    coach.delete_pdf(a)
    assert files.deleted == ["file_1"] and [u["filename"] for u in store.list_pdfs(conn, pid)] == ["b.pdf"]
    coach.delete_subject(pid)
    assert files.deleted == ["file_1", "file_2"]


def test_cost_estimate_counts_visual_pages(coach, conn):
    text_pid, visual_pid = store.create_profile(conn), store.create_profile(conn)
    ingest_pdf(conn, text_pid, "t.pdf", make_pdf([TEXT] * 4))
    ingest_pdf(conn, visual_pid, "v.pdf", make_pdf([MATH] * 4))
    assert store.pdf_visual_page_count(conn, visual_pid) == 4
    text_cost = coach._formula_costs(text_pid)["teacher"]
    visual_cost = coach._formula_costs(visual_pid)["teacher"]
    # 4 visual pages x 4,500 tokens at Sonnet's cache-write price, vs ~4 short text pages
    assert visual_cost - text_cost > 4 * 4000 * config.PRICES[config.SONNET]["cache_write"] / 1e6


# ---------- migration ----------

def test_phase4_database_gets_the_new_pdf_columns(tmp_path):
    path = tmp_path / "coach.sqlite"
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE pdf_uploads (id INTEGER PRIMARY KEY AUTOINCREMENT, profile_id INTEGER NOT NULL,
            filename TEXT NOT NULL, page_from INTEGER, page_to INTEGER, n_pages INTEGER NOT NULL,
            n_text_pages INTEGER NOT NULL, uploaded_at TEXT NOT NULL);
        CREATE TABLE pdf_pages (upload_id INTEGER NOT NULL, page_no INTEGER NOT NULL, text TEXT NOT NULL,
            PRIMARY KEY (upload_id, page_no));
        INSERT INTO pdf_uploads (profile_id, filename, n_pages, n_text_pages, uploaded_at)
            VALUES (1, 'old.pdf', 1, 1, '2026-10-01T10:00:00');
        INSERT INTO pdf_pages VALUES (1, 1, 'old text');
    """)
    old.close()

    conn = store.connect(path)
    assert {"data"} <= {r["name"] for r in conn.execute("PRAGMA table_info(pdf_uploads)")}
    assert {"kind", "file_id"} <= {r["name"] for r in conn.execute("PRAGMA table_info(pdf_pages)")}
    assert store.list_pdfs(conn, 1)[0]["has_data"] == 0  # old upload: text only
    assert len(list(tmp_path.glob("coach.backup-*.sqlite"))) == 1
    conn.close()
