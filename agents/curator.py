"""Curator: finds 1-2 resources per milestone with web search (Haiku).

Two API calls, because citations (always on for web search) can't be combined with
structured outputs:
  1. search call: Claude searches the web and writes notes about the best candidates.
  2. pick call: Claude turns the notes into JSON (CuratorDraft). No tools.
Then Python drops any URL that did not appear in the real search results, so the
Curator can't invent links.
"""
from typing import Literal

from pydantic import BaseModel

import config

SEARCH_SYSTEM = """You are the Curator in a personal learning app. Use web search to find the
best learning resources for ONE milestone objective, matched to the learner's subject profile
(level, tools and versions, units, available time).

Rules:
- Prefer official documentation, well-known tutorials and textbooks over forums and SEO pages.
- Prefer text pages (articles, docs, PDFs). Videos are fine as an extra, but include at least one
  text page whenever one exists.
- For each candidate, find the EXACT part to use: a section heading for pages, page numbers
  for PDFs, start-end timestamps for videos. Estimate the minutes needed.
- Also find 1-2 backup candidates in case a primary page can't be downloaded.

When done searching, write short notes listing each candidate: URL, title, kind
(page/video/pdf), exact section or timestamps, minutes, and why it beats the alternatives.
"""

PICK_SYSTEM = """You are the Curator in a personal learning app. From the search notes and the
list of URLs that were actually found, choose the resources for the milestone:
- 1 or 2 "primary" resources (the ones the learner should use), at least one of them a text
  page or PDF if any exist,
- up to 2 "backup" resources (used if a primary can't be downloaded).
Use ONLY URLs from the FOUND URLS list, copied exactly. For each one give the exact
section/page numbers/timestamps, estimated minutes, and why it beats the alternatives.
"""


def with_blocked_note(system: str) -> str:
    """Add the list of sites that refuse downloads, so the Curator prefers other sources."""
    if not config.BLOCKED_DOMAINS:
        return system
    sites = ", ".join(config.BLOCKED_DOMAINS)
    return (system + f"\nThese sites can't be downloaded by the app: {sites}. Prefer other sources. "
            "Only use them if nothing comparable exists elsewhere, and then as a backup, never as "
            "the only text page.\n")


# Shape sent to the API.
class ResourceDraft(BaseModel):
    url: str
    title: str
    kind: Literal["page", "video", "pdf"]
    section: str
    minutes: float
    why_best: str
    role: Literal["primary", "backup"]


class CuratorDraft(BaseModel):
    resources: list[ResourceDraft]


def _norm(url: str) -> str:
    return url.strip().rstrip("/").lower()


def search_results(blocks) -> tuple[dict[str, str], str, list[str]]:
    """From the search call's content blocks, return (found {url: title}, notes text, errors)."""
    found, notes, errors = {}, [], []
    for b in blocks:
        if b.type == "web_search_tool_result":
            if isinstance(b.content, list):
                for r in b.content:
                    found.setdefault(r.url, r.title)
            else:  # an error object, e.g. max_uses_exceeded
                errors.append(getattr(b.content, "error_code", "unknown error"))
        elif b.type == "text":
            notes.append(b.text)
    return found, "".join(notes), errors


def check_resources(draft: CuratorDraft, found: dict[str, str]) -> tuple[list[dict], list[str]]:
    """Keep only real URLs; at most 2 primaries and 2 backups. Returns (resources, dropped_urls)."""
    real = {_norm(u): u for u in found}
    kept, dropped = [], []
    for r in draft.resources:
        if _norm(r.url) not in real:
            dropped.append(r.url)
            continue
        kept.append({**r.model_dump(), "url": real[_norm(r.url)]})

    primaries = [r for r in kept if r["role"] == "primary"]
    backups = [r for r in kept if r["role"] == "backup"]
    for extra in primaries[2:]:  # more than 2 primaries: the extras become backups
        extra["role"] = "backup"
    backups = primaries[2:] + backups
    primaries = primaries[:2]
    if not primaries and backups:  # no real primary left: promote the first backup
        backups[0]["role"] = "primary"
        primaries, backups = backups[:1], backups[1:]
    return primaries + backups[:2], dropped


def find_resources(llm, profile: dict, milestone: dict, profile_id=None) -> tuple[list[dict], dict]:
    """Return (resources, info). info has the search errors and dropped (invented) URLs."""
    request = (f"Milestone: {milestone['title']}\nObjective: {milestone['objective']}\n"
               "Find the best resources for this objective.")
    tool = {"type": config.WEB_SEARCH_TOOL, "name": "web_search", "max_uses": config.WEB_SEARCH_MAX_USES}
    blocks, _ = llm.call_raw("curator_search", with_blocked_note(SEARCH_SYSTEM),
                             [{"role": "user", "content": request}],
                             tools=[tool], profile=profile, profile_id=profile_id)
    found, notes, errors = search_results(blocks)
    info = {"search_errors": errors, "dropped_urls": [], "found": len(found)}
    if not found:
        return [], info

    url_list = "\n".join(f"- {u} ({t})" for u, t in found.items())
    pick_request = f"{request}\n\nSEARCH NOTES:\n{notes}\n\nFOUND URLS:\n{url_list}"
    draft = llm.call_structured("curator", with_blocked_note(PICK_SYSTEM),
                                [{"role": "user", "content": pick_request}],
                                CuratorDraft, profile=profile, profile_id=profile_id)
    resources, dropped = check_resources(draft, found)
    info["dropped_urls"] = dropped
    return resources, info


def is_long(resource: dict) -> bool:
    return (resource.get("minutes") or 0) > config.LONG_RESOURCE_MIN
