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
