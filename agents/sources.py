"""Where lesson material comes from. Pluggable so Phase 2 can add PDFs and web sources.

Rules (used from Phase 2 on):
- Uploaded PDFs win over web sources, and both win over model knowledge when they conflict.
- Every lesson section is labeled with its source kind: pdf, web or model.
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
    ref: str  # file name + page, URL + section, or model ID


class LessonSource:
    """Base class. Each source type returns chunks relevant to one objective."""

    kind: SourceKind = "model"

    def get_chunks(self, objective: str, profile: dict) -> list[SourceChunk]:
        raise NotImplementedError


class ModelKnowledgeSource(LessonSource):
    """Phase 1: no external material. The Teacher writes from its own knowledge."""

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
