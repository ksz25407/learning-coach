import pytest

import config
from db import store
from llm import (LLMError, BudgetExceeded, RefusalError, compute_cost, price_for, system_blocks)
from tests.conftest import usage


def test_cost_sonnet():
    # 1M input @ $2 + 1M output @ $10 + 1M cache write @ $2.50 + 1M cache read @ $0.20
    u = usage(1_000_000, 1_000_000, 1_000_000, 1_000_000)
    assert compute_cost("claude-sonnet-5-5", u) == pytest.approx(14.70)


def test_cost_small_call():
    # 1000 in, 500 out on Sonnet = 0.002 + 0.005
    assert compute_cost("claude-sonnet-5-5", usage(1000, 500)) == pytest.approx(0.007)


def test_unknown_model_uses_highest_price():
    assert price_for("some-new-model") == config.PRICES[config.OPUS]


def test_cost_uses_model_that_answered(llm, fake, conn):
    """Configured model is Sonnet, but the reply came from Opus -> priced as Opus."""
    fake.messages.model = "claude-opus-5-5"
    fake.messages.text_replies = ["hi"]
    llm.call_text("coach", "sys", [{"role": "user", "content": "hello"}])
    row = conn.execute("SELECT model, cost_usd FROM usage_log").fetchone()
    assert row["model"] == "claude-opus-5-5"
    assert row["cost_usd"] == pytest.approx(compute_cost("claude-opus-5-5", usage()))
    assert row["cost_usd"] != pytest.approx(compute_cost("claude-sonnet-5-5", usage()))


def test_budget_blocks_call_at_stop_limit(llm, fake, conn):
    store.log_usage(conn, None, "coach", "m", 1, 1, 0, 0, config.STOP_USD)
    fake.messages.text_replies = ["hi"]
    with pytest.raises(BudgetExceeded):
        llm.call_text("coach", "sys", [{"role": "user", "content": "hello"}])
    assert fake.messages.calls == []  # nothing was sent


def test_budget_allows_call_below_limit(llm, fake, conn):
    store.log_usage(conn, None, "coach", "m", 1, 1, 0, 0, config.STOP_USD - 0.01)
    fake.messages.text_replies = ["hi"]
    text, _ = llm.call_text("coach", "sys", [{"role": "user", "content": "hello"}])
    assert text == "hi"


def test_refusal_raises_but_is_still_logged(llm, fake, conn):
    fake.messages.stop_reason = "refusal"
    fake.messages.text_replies = [""]
    with pytest.raises(RefusalError):
        llm.call_text("coach", "sys", [{"role": "user", "content": "hello"}])
    assert conn.execute("SELECT COUNT(*) FROM usage_log").fetchone()[0] == 1


def test_large_prompt_is_counted_and_rejected(llm, fake):
    big = "x" * (config.TOKEN_COUNT_THRESHOLD * 4 + 100)
    with pytest.raises(LLMError):
        llm.call_text("coach", "sys", [{"role": "user", "content": big}])
    assert fake.messages.calls[0][0] == "count_tokens"


def test_request_shape(llm, fake):
    fake.messages.text_replies = ["hi"]
    llm.call_text("teacher", "sys", [{"role": "user", "content": "q"}], profile={"subject": "Revit"})
    kwargs = fake.messages.calls[0][1]
    assert kwargs["model"] == config.MODELS["teacher"]
    assert kwargs["output_config"] == {"effort": config.EFFORT["teacher"]}
    assert "thinking" not in kwargs  # no budget_tokens on these models
    assert "extra_body" not in kwargs  # fallbacks off by default


def test_system_blocks_cache_stable_prefix_only():
    blocks = system_blocks("prompt", {"b": 1, "a": 2}, extra="volatile")
    assert blocks[0] == {"type": "text", "text": "prompt"}
    assert '"a": 2, "b": 1' in blocks[1]["text"]  # sorted keys -> stable bytes
    assert blocks[1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in blocks[2]  # milestone status etc. not cached
