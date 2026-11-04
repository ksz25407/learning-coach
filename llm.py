"""The one place that talks to the Claude API.

Every agent call goes through LLM.call_structured() or LLM.call_text(), which:
  1. block the call if this month's spend has reached STOP_USD,
  2. put the stable system prompt + subject profile first and mark it for caching,
  3. count tokens first when the prompt looks large,
  4. log tokens and cost (priced by the model that actually answered) to usage_log,
  5. raise a clear error on refusals or cut-off replies.

Two checkers can run at the same time (threads). They share one SQLite connection, so the
budget check and the usage logging are done under a lock.
"""
import json
import logging
import threading

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


def web_searches(usage) -> int:
    """Number of web searches in this call (0 if the field is missing)."""
    server = getattr(usage, "server_tool_use", None)
    return (getattr(server, "web_search_requests", 0) or 0) if server else 0


def compute_cost(model: str, usage) -> float:
    """Token cost + web search fees, in USD."""
    p = price_for(model)
    tokens = (
        (usage.input_tokens or 0) * p["input"]
        + (usage.output_tokens or 0) * p["output"]
        + (getattr(usage, "cache_creation_input_tokens", 0) or 0) * p["cache_write"]
        + (getattr(usage, "cache_read_input_tokens", 0) or 0) * p["cache_read"]
    ) / 1_000_000
    return tokens + web_searches(usage) * config.WEB_SEARCH_USD


# ---------- client wrapper ----------

class LLM:
    def __init__(self, conn, client=None):
        self.conn = conn
        self._client = client  # tests pass a fake client
        self._db_lock = threading.Lock()

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

    def call_raw(self, agent, system, messages, tools=None, profile=None, extra=None, profile_id=None):
        """Return (content_blocks, model_that_answered). Keeps citations and tool blocks.

        Server tools (web search) can pause a long turn (stop_reason "pause_turn"); we send
        the partial answer back so the API continues it, up to PAUSE_TURN_MAX_ROUNDS times.
        """
        kwargs = self._build(agent, system, messages, profile, extra)
        if tools:
            kwargs["tools"] = tools
        blocks = []
        for _ in range(config.PAUSE_TURN_MAX_ROUNDS):
            response = self.client.messages.create(**kwargs)
            self._finish(agent, response, profile_id)
            blocks.extend(response.content)
            if response.stop_reason != "pause_turn":
                return blocks, response.model
            kwargs["messages"] = list(messages) + [{"role": "assistant", "content": blocks}]
        raise LLMError(f"{agent}: still paused after {config.PAUSE_TURN_MAX_ROUNDS} rounds.")

    # --- Files API (Phase 5: visual PDF pages) ---

    def upload_file(self, filename: str, data: bytes, media_type: str = "application/pdf") -> str:
        """Upload a file once; later requests refer to it by id instead of resending it.
        Uploading is free (the file's tokens are billed when a request uses it)."""
        try:
            return self.client.files.upload(file=(filename, data, media_type)).id
        except Exception as e:  # network or API error: same handling as a failed call
            raise LLMError(f"Couldn't upload {filename} to the Files API: {e}") from e

    def delete_file(self, file_id: str) -> bool:
        """Delete an uploaded file. Never raises: a file that can't be deleted is only logged."""
        try:
            self.client.files.delete(file_id)
            return True
        except Exception as e:
            log.warning("Couldn't delete file %s: %s", file_id, e)
            return False

    # --- internals ---

    def _build(self, agent, system, messages, profile, extra):
        with self._db_lock:
            spent = store.month_spend(self.conn)
        if spent >= config.STOP_USD:
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
        """Count tokens first if the prompt looks large. count_tokens can't read Files API
        documents, so those are left out of the count and VISUAL_PAGE_TOKENS is added per file."""
        messages, n_files = without_file_documents(kwargs["messages"])
        extra = n_files * config.VISUAL_PAGE_TOKENS
        rough = (len(json.dumps(kwargs["system"])) + len(json.dumps(messages))) / 4 + extra
        if rough < config.TOKEN_COUNT_THRESHOLD:
            return
        count = self.client.messages.count_tokens(
            model=kwargs["model"], system=kwargs["system"], messages=messages
        )
        total = count.input_tokens + extra
        if total > config.MAX_INPUT_TOKENS:
            raise LLMError(f"Prompt is about {total} tokens (limit {config.MAX_INPUT_TOKENS}). Not sent.")

    def _finish(self, agent, response, profile_id):
        u = response.usage
        with self._db_lock:
            self._log(agent, response, u, profile_id)
        if response.stop_reason == "refusal":
            raise RefusalError(f"{agent}: the model declined this request.")
        if response.stop_reason == "max_tokens":
            raise LLMError(f"{agent}: reply was cut off (max_tokens). Raise MAX_TOKENS['{agent}'].")

    def _log(self, agent, response, u, profile_id):
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


def without_file_documents(messages: list[dict]) -> tuple[list[dict], int]:
    """(messages without Files API document blocks, how many were removed)."""
    out, n = [], 0
    for m in messages:
        content = m.get("content")
        if isinstance(content, list):
            kept = [b for b in content if not (isinstance(b, dict) and b.get("type") == "document"
                                              and b.get("source", {}).get("type") == "file")]
            n += len(content) - len(kept)
            m = {**m, "content": kept or [{"type": "text", "text": "(documents)"}]}
        out.append(m)
    return out, n


def system_blocks(system: str, profile: dict | None, extra: str | None = None) -> list[dict]:
    """Stable prefix first (prompt + profile, cached), then volatile `extra` (not cached)."""
    blocks = [{"type": "text", "text": system}]
    if profile is not None:
        blocks.append({"type": "text", "text": "Subject profile:\n" + json.dumps(profile, sort_keys=True)})
    blocks[-1]["cache_control"] = {"type": "ephemeral"}
    if extra:
        blocks.append({"type": "text", "text": extra})
    return blocks
