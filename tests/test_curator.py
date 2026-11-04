"""Curator: web search call + structured pick call, and the Python checks on its output."""
import pytest

import config
from agents.curator import CuratorDraft, ResourceDraft, check_resources, find_resources, is_long
from tests.conftest import reply, search_error, search_results, text
from tests.test_agents import PROFILE

MILESTONE = {"title": "Walls", "objective": "Model the ground-floor walls from a sketch in 20 minutes"}
FOUND = {"https://docs.example.com/walls": "Walls guide",
         "https://example.com/video": "Walls video",
         "https://blog.example.com/tips": "Tips"}


def r(url, role="primary", kind="page", minutes=10):
    return ResourceDraft(url=url, title="t", kind=kind, section="Section 2", minutes=minutes,
                         why_best="official", role=role)


def test_two_calls_and_real_urls_only(llm, fake, conn):
    fake.messages.text_replies = [reply(text("Searching..."), search_results(FOUND),
                                        text("Notes: the docs page is best."), searches=2)]
    fake.messages.parse_replies = [CuratorDraft(resources=[
        r("https://docs.example.com/walls/"),           # trailing slash: still matches
        r("https://invented.example.com/fake"),          # not in search results: dropped
        r("https://blog.example.com/tips", role="backup"),
    ])]
    resources, info = find_resources(llm, PROFILE, MILESTONE)

    assert [c[0] for c in fake.messages.calls] == ["create", "parse"]
    search_call = fake.messages.calls[0][1]
    assert search_call["tools"] == [{"type": config.WEB_SEARCH_TOOL, "name": "web_search",
                                     "max_uses": config.WEB_SEARCH_MAX_USES}]
    assert search_call["model"] == config.MODELS["curator_search"]
    assert "tools" not in fake.messages.calls[1][1]  # no tools with structured output
    pick_prompt = fake.messages.calls[1][1]["messages"][0]["content"]
    assert "https://docs.example.com/walls" in pick_prompt and "Notes: the docs page" in pick_prompt

    assert [x["url"] for x in resources] == ["https://docs.example.com/walls", "https://blog.example.com/tips"]
    assert info["dropped_urls"] == ["https://invented.example.com/fake"]


def test_search_cost_is_logged(llm, fake, conn):
    fake.messages.text_replies = [reply(text("x"), search_results(FOUND), searches=3)]
    fake.messages.parse_replies = [CuratorDraft(resources=[r("https://docs.example.com/walls")])]
    find_resources(llm, PROFILE, MILESTONE)
    row = conn.execute("SELECT cost_usd FROM usage_log WHERE agent = 'curator_search'").fetchone()
    assert row["cost_usd"] >= 3 * config.WEB_SEARCH_USD


def test_no_results_skips_pick_call(llm, fake):
    fake.messages.text_replies = [reply(text("Search failed."), search_error("too_many_requests"))]
    resources, info = find_resources(llm, PROFILE, MILESTONE)
    assert resources == []
    assert info["search_errors"] == ["too_many_requests"]
    assert len(fake.messages.calls) == 1


def test_extra_primaries_become_backups():
    found = {f"https://e.com/{i}": "t" for i in range(5)}
    draft = CuratorDraft(resources=[r(f"https://e.com/{i}") for i in range(4)])
    resources, _ = check_resources(draft, found)
    assert [x["role"] for x in resources] == ["primary", "primary", "backup", "backup"]


def test_backup_promoted_when_no_primary_left():
    found = {"https://e.com/b": "t"}
    draft = CuratorDraft(resources=[r("https://fake.com/p"), r("https://e.com/b", role="backup")])
    resources, dropped = check_resources(draft, found)
    assert resources[0]["role"] == "primary" and resources[0]["url"] == "https://e.com/b"
    assert dropped == ["https://fake.com/p"]


@pytest.mark.parametrize("minutes,flag", [(30, False), (31, True), (None, False)])
def test_long_resources_flagged(minutes, flag):
    assert is_long({"minutes": minutes}) is flag
