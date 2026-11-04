"""Shared test helpers: a temp database and a fake Anthropic client (no real API calls)."""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import store  # noqa: E402
from llm import LLM  # noqa: E402


def usage(inp=100, out=50, cw=0, cr=0, searches=0):
    return SimpleNamespace(input_tokens=inp, output_tokens=out,
                           cache_creation_input_tokens=cw, cache_read_input_tokens=cr,
                           server_tool_use=SimpleNamespace(web_search_requests=searches))


# ---- builders for fake content blocks ----

def text(t, citations=None):
    return SimpleNamespace(type="text", text=t, citations=citations)


def cite(doc_index, cited_text="quoted text"):
    return SimpleNamespace(type="char_location", document_index=doc_index, cited_text=cited_text,
                           start_char_index=0, end_char_index=len(cited_text))


def search_results(found: dict):
    """A web_search_tool_result block listing {url: title}."""
    return SimpleNamespace(type="web_search_tool_result", content=[
        SimpleNamespace(type="web_search_result", url=u, title=t) for u, t in found.items()])


def search_error(code="max_uses_exceeded"):
    return SimpleNamespace(type="web_search_tool_result",
                           content=SimpleNamespace(type="web_search_tool_result_error", error_code=code))


def reply(*blocks, stop_reason="end_turn", searches=0):
    """A create() reply with content blocks, a stop reason and a web search count."""
    return {"content": list(blocks), "stop_reason": stop_reason, "searches": searches}


class FakeMessages:
    """Replays queued replies. parse() replies are Pydantic objects. create() replies are
    strings (one text block) or reply(...) dicts (blocks, stop reason, search count)."""

    def __init__(self):
        self.parse_replies = []
        self.text_replies = []
        self.calls = []
        self.model = "claude-sonnet-5-5"
        self.stop_reason = "end_turn"

    def parse(self, **kwargs):
        self.calls.append(("parse", kwargs))
        return SimpleNamespace(parsed_output=self.parse_replies.pop(0), usage=usage(),
                               model=self.model, stop_reason=self.stop_reason, content=[])

    def create(self, **kwargs):
        self.calls.append(("create", kwargs))
        r = self.text_replies.pop(0)
        if isinstance(r, str):
            r = reply(text(r), stop_reason=self.stop_reason)
        return SimpleNamespace(content=r["content"], usage=usage(searches=r["searches"]),
                               model=self.model, stop_reason=r["stop_reason"])

    def count_tokens(self, **kwargs):
        self.calls.append(("count_tokens", kwargs))
        return SimpleNamespace(input_tokens=999_999)


@pytest.fixture
def conn(tmp_path):
    c = store.connect(tmp_path / "test.sqlite")
    yield c
    c.close()


@pytest.fixture
def fake():
    return SimpleNamespace(messages=FakeMessages())


@pytest.fixture
def llm(conn, fake):
    return LLM(conn, client=fake)
