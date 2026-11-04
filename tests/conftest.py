"""Shared test helpers: a temp database and a fake Anthropic client (no real API calls)."""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import store  # noqa: E402
from llm import LLM  # noqa: E402


def usage(inp=100, out=50, cw=0, cr=0):
    return SimpleNamespace(input_tokens=inp, output_tokens=out,
                           cache_creation_input_tokens=cw, cache_read_input_tokens=cr)


class FakeMessages:
    """Replays queued replies. parse() replies are Pydantic objects, create() replies are strings."""

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
        text = self.text_replies.pop(0)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], usage=usage(),
                               model=self.model, stop_reason=self.stop_reason)

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
