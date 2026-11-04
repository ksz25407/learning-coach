"""Teacher: writes one lesson for one milestone. Returns prose (not JSON)."""
import re

from agents.sources import ModelKnowledgeSource, SourceChunk, gather

SYSTEM = """You are the Teacher in a personal learning app. Write ONE lesson that gets the
learner to pass the milestone objective. Use the subject profile to match their level,
tools/versions, units and available time.

Format (Markdown):
- Split the lesson into sections. Each section starts with a "## " heading.
- The line right after each heading is a source tag: [Source: pdf], [Source: web] or [Source: model].
  Use [Source: model] when the section comes from your own knowledge.
- If SOURCE MATERIAL is given, prefer it. PDF material wins over web material, and both win
  over your own knowledge when they disagree.
- End with a "## Practice" section: a short task that mirrors the objective.

Be concrete and brief. Teach only what the objective needs. If you are unsure about a
detail (menu names, version-specific behaviour), say so instead of guessing.
"""

SOURCE_TAG = re.compile(r"^\[Source:\s*(pdf|web|model)\]", re.IGNORECASE)


def ensure_source_tags(text: str, default: str = "model") -> str:
    """Add "[Source: <default>]" under any "## " heading that is missing a tag."""
    lines = text.splitlines()
    out = []
    for i, line in enumerate(lines):
        out.append(line)
        if line.startswith("## "):
            nxt = next((l for l in lines[i + 1:] if l.strip()), "")
            if not SOURCE_TAG.match(nxt.strip()):
                out.append(f"[Source: {default}]")
    return "\n".join(out)


def format_chunks(chunks: list[SourceChunk]) -> str:
    parts = [f"[{c.kind} | {c.ref}]\n{c.text}" for c in chunks]
    return "SOURCE MATERIAL (highest priority first):\n\n" + "\n\n".join(parts)


def write_lesson(llm, profile: dict, milestone: dict, sources=None, profile_id=None):
    """Return (lesson_text, sources_list, model_that_answered)."""
    sources = sources or [ModelKnowledgeSource()]
    chunks = gather(sources, milestone["objective"], profile)

    request = f"Milestone: {milestone['title']}\nObjective: {milestone['objective']}"
    if chunks:
        request = format_chunks(chunks) + "\n\n" + request
    messages = [{"role": "user", "content": request}]

    text, model = llm.call_text("teacher", SYSTEM, messages, profile=profile, profile_id=profile_id)

    if not chunks:  # Phase 1: everything is from model knowledge
        text = ensure_source_tags(text, default="model")
        used = [{"kind": "model", "ref": model}]
    else:
        used = [{"kind": c.kind, "ref": c.ref} for c in chunks]
    return text, used, model
