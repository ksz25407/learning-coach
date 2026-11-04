"""Where lesson material comes from. Each source type is a small class with get_chunks().

Rules:
- Uploaded PDFs win over web pages, and both win over model knowledge when they conflict.
- One chunk = one document the Teacher can cite (one PDF page, or one web page).
- Every lesson section is labeled with the source kinds it actually cites: pdf, web or model.
"""
from dataclasses import dataclass
from typing import Literal

SourceKind = Literal["pdf", "web", "model"]

# Lower number = higher priority.
PRIORITY = {"pdf": 0, "web": 1, "model": 2}


@dataclass
class SourceChunk:
    text: str
    kind: SourceKind
    ref: str  # "manual.pdf p.47", a URL, or a model ID
    title: str = ""  # shown to the Teacher and in the Sources list (defaults to ref)
    # Phase 5: a PDF page sent visually (Claude reads its text and an image of it). Files API
    # id of the page as a 1-page PDF; "" = send `text` as a plain text document.
    file_id: str = ""


def document_source(chunk: SourceChunk) -> dict:
    """The `source` of a document block: the uploaded 1-page PDF for a visual page, else text."""
    if chunk.file_id:
        return {"type": "file", "file_id": chunk.file_id}
    return {"type": "text", "media_type": "text/plain", "data": chunk.text}


# Put on the first block after the source documents: everything before it (system prompt,
# profile, documents) is cached, so revision rounds and re-checks pay ~10% for the documents.
CACHE_HERE = {"type": "ephemeral"}


def plain_document(chunk: SourceChunk) -> dict:
    """A source document without citations (for agents that reply in JSON: citations and
    structured outputs can't be combined)."""
    return {
        "type": "document",
        "source": document_source(chunk),
        "title": chunk.title or chunk.ref,
        "context": f"Source kind: {chunk.kind}. Reference: {chunk.ref}",
    }


def source_documents(chunks: list[SourceChunk]) -> list[dict]:
    """The documents, then a short marker block that ends the cached prefix. [] if no chunks."""
    if not chunks:
        return []
    return [plain_document(c) for c in chunks] + [
        {"type": "text", "text": "The documents above are the lesson's sources.", "cache_control": CACHE_HERE}]


class LessonSource:
    """Base class. Each source type returns chunks relevant to one objective.

    `notes` collects what happened (pages picked, URLs skipped and why) so the UI can show it.
    """

    kind: SourceKind = "model"

    def __init__(self):
        self.notes: list[dict] = []

    def get_chunks(self, objective: str, profile: dict) -> list[SourceChunk]:
        raise NotImplementedError


class ModelKnowledgeSource(LessonSource):
    """No external material. The Teacher writes from its own knowledge."""

    kind = "model"

    def get_chunks(self, objective: str, profile: dict) -> list[SourceChunk]:
        return []


def merge_sources(chunks: list[SourceChunk]) -> list[SourceChunk]:
    """Order chunks pdf > web > model (stable within each kind)."""
    return sorted(chunks, key=lambda c: PRIORITY[c.kind])


def gather(sources: list[LessonSource], objective: str, profile: dict) -> list[SourceChunk]:
    chunks = []
    for s in sources:
        chunks.extend(s.get_chunks(objective, profile))
    return merge_sources(chunks)
