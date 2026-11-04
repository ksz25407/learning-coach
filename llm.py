"""The one place that talks to the Claude API.

Every agent call goes through LLM.call_structured() or LLM.call_text(), which:
  1. block the call if this month's spend has reached STOP_USD,
  2. put the stable system prompt + subject profile first and mark it for caching,
  3. count tokens first when the prompt looks large,
  4. log tokens and cost (priced by the model that actually answered) to usage_log,
  5. raise a clear error on refusals or cut-off replies.
"""
import json
import logging

import config
from db import store

log = logging.getLogger(__name__)


class BudgetExceeded(Exception):
    pass


class RefusalError(Exception):
    pass


class LLMError(Exception):
    pass


# ---------- cost ----------

def price_for(model: str) -> dict:
    """Find prices for the model that answered. Unknown model -> most expensive prices."""
    for name, prices in config.PRICES.items():
        if model == name or model.startswith(name + "-"):
            return prices
    log.warning("No price for model %r; charging the highest listed price.", model)
    return max(config.PRICES.values(), key=lambda p: p["output"])


def compute_cost(model: str, usage) -> float:
    p = price_for(model)
    return (
        (usage.input_tokens or 0) * p["input"]
        + (usage.output_tokens or 0) * p["output"]
        + (getattr(usage, "cache_creation_input_tokens", 0) or 0) * p["cache_write"]
        + (getattr(usage, "cache_read_input_tokens", 0) or 0) * p["cache_read"]
    ) / 1_000_000


# ---------- client wrapper ----------

class LLM:
    def __init__(self, conn, client=None):
        self.conn = conn
        self._client = client  # tests pass a fake client

    @property
    def client(self):
        if self._client is None:
            import anthropic
            from dotenv import load_dotenv

            load_dotenv(config.ROOT / ".env")
            self._client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
        return self._client

    # --- public ---

    def call_structured(self, agent, system, messages, schema, profile=None, extra=None, profile_id=None):
        """Return a validated Pydantic object of type `schema`."""
        kwargs = self._build(agent, system, messages, profile, extra)
        response = self.client.messages.parse(output_format=schema, **kwargs)
        self._finish(agent, response, profile_id)
        if response.parsed_output is None:
            raise LLMError(f"{agent}: reply could not be parsed")
        return response.parsed_output

    def call_text(self, agent, system, messages, profile=None, extra=None, profile_id=None):
        """Return (text, model_that_answered)."""
        kwargs = self._build(agent, system, messages, profile, extra)
        response = self.client.messages.create(**kwargs)
        self._finish(agent, response, profile_id)
        text = "".join(b.text for b in response.content if b.type == "text")
        return text, response.model

    # --- internals ---

    def _build(self, agent, system, messages, profile, extra):
        if store.month_spend(self.conn) >= config.STOP_USD:
            raise BudgetExceeded(
                f"This month's spend has reached ${config.STOP_USD:.2f}. "
                "Raise STOP_USD in config.py to continue."
            )
        kwargs = {
            "model": config.MODELS[agent],
            "max_tokens": config.MAX_TOKENS[agent],
            "system": system_blocks(system, profile, extra),
            "messages": messages,
            "output_config": {"effort": config.EFFORT[agent]},
        }
        if config.USE_SERVER_FALLBACKS:
            kwargs["extra_headers"] = {"anthropic-beta": config.FALLBACK_BETA}
            kwargs["extra_body"] = {"fallbacks": "default"}
        self._check_size(kwargs)
        return kwargs

    def _check_size(self, kwargs):
        rough = len(json.dumps(kwargs["system"])) + len(json.dumps(kwargs["messages"]))
        if rough / 4 < config.TOKEN_COUNT_THRESHOLD:
            return
        count = self.client.messages.count_tokens(
            model=kwargs["model"], system=kwargs["system"], messages=kwargs["messages"]
        )
        if count.input_tokens > config.MAX_INPUT_TOKENS:
            raise LLMError(
                f"Prompt is {count.input_tokens} tokens (limit {config.MAX_INPUT_TOKENS}). Not sent."
            )

    def _finish(self, agent, response, profile_id):
        u = response.usage
        store.log_usage(
            self.conn,
            profile_id=profile_id,
            agent=agent,
            model=response.model,
            input_tokens=u.input_tokens or 0,
            output_tokens=u.output_tokens or 0,
            cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
            cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
            cost_usd=compute_cost(response.model, u),
        )
        if response.stop_reason == "refusal":
            raise RefusalError(f"{agent}: the model declined this request.")
        if response.stop_reason == "max_tokens":
            raise LLMError(f"{agent}: reply was cut off (max_tokens). Raise MAX_TOKENS['{agent}'].")


def system_blocks(system: str, profile: dict | None, extra: str | None = None) -> list[dict]:
    """Stable prefix first (prompt + profile, cached), then volatile `extra` (not cached)."""
    blocks = [{"type": "text", "text": system}]
    if profile is not None:
        blocks.append({"type": "text", "text": "Subject profile:\n" + json.dumps(profile, sort_keys=True)})
    blocks[-1]["cache_control"] = {"type": "ephemeral"}
    if extra:
        blocks.append({"type": "text", "text": extra})
    return blocks
