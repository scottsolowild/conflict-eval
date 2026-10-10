"""One adapter per provider.

An adapter builds the exact request body for a conversation, sends it, and
reads the reply back out of the raw response. The cache stores the raw
response, so `parse` runs the same way on a fresh call and on a cached one.

A reply carries the provider's own form of the assistant turn. The second
turn replays it as the provider returned it, thinking blocks and thought
signatures included, which is how a real chat client continues a
conversation.

No adapter sends a system prompt to a target. Each one measures the model
as the API serves it. The judge does take a system prompt and a JSON schema.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any


class ProviderError(RuntimeError):
    pass


@dataclass
class Reply:
    text: str
    turn: Any                 # the assistant turn in the provider's own shape
    stop: str                 # why the model stopped, in the provider's words
    usage: dict = field(default_factory=dict)   # {"input": n, "output": n}, output includes thinking
    model: str = ""           # the model that answered, as the provider reports it

    @property
    def refused(self) -> bool:
        return self.stop in ("refusal", "SAFETY", "content_filter") or not self.text


def post_json(url: str, body: dict, headers: dict, tries: int = 4, timeout: int = 600) -> dict:
    """POST with a short retry on rate limits and server errors. The URL never
    carries a key, so an error message is safe to print."""
    data = json.dumps(body).encode("utf-8")
    for attempt in range(tries):
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Content-Type": "application/json", **headers},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:600]
            # A quota with no room ("limit: 0", billing off) won't clear on a retry.
            out_of_quota = e.code == 429 and "exceeded your current quota" in detail
            if e.code in (429, 500, 502, 503, 504) and attempt < tries - 1 and not out_of_quota:
                time.sleep(5 * 2 ** attempt)
                continue
            raise ProviderError(f"{url} returned {e.code}: {detail}") from None
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < tries - 1:
                time.sleep(5 * 2 ** attempt)
                continue
            raise ProviderError(f"{url} unreachable: {e}") from None
    raise ProviderError(f"{url}: out of retries")


class Provider:
    name = ""
    env_key = ""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.model = cfg.get("model")

    @property
    def namespace(self) -> str:
        return f"{self.name}/{self.model}"

    def missing(self) -> str | None:
        """Why this provider can't run, or None when it can."""
        if not self.model:
            return "no model id in config.yaml"
        if self.env_key and not os.environ.get(self.env_key):
            return f"{self.env_key} is not set"
        return None

    def request(self, turns: list, system: str | None = None, schema: dict | None = None) -> dict:
        raise NotImplementedError

    def send(self, request: dict) -> dict:
        raise NotImplementedError

    def parse(self, raw: dict) -> Reply:
        raise NotImplementedError


class Anthropic(Provider):
    name = "anthropic"
    env_key = "ANTHROPIC_API_KEY"
    _client = None

    def request(self, turns, system=None, schema=None):
        req = {
            "model": self.model,
            "max_tokens": self.cfg.get("max_tokens", 16000),
            "messages": [{"role": role, "content": content} for role, content in turns],
        }
        output_config = {}
        if self.cfg.get("effort"):
            output_config["effort"] = self.cfg["effort"]
        if schema:
            output_config["format"] = {"type": "json_schema", "schema": schema}
        if output_config:
            req["output_config"] = output_config
        if system:
            req["system"] = system
        return req

    def send(self, request):
        import anthropic
        if Anthropic._client is None:
            Anthropic._client = anthropic.Anthropic(max_retries=4)
        try:
            return Anthropic._client.messages.create(**request).to_dict(mode="json")
        except anthropic.APIStatusError as e:
            raise ProviderError(f"Anthropic API returned {e.status_code}: {e.message}") from None
        except anthropic.APIConnectionError as e:
            raise ProviderError(f"Anthropic API unreachable: {e}") from None

    def parse(self, raw):
        turn = []
        for block in raw.get("content", []):
            kind = block.get("type")
            if kind == "text":
                turn.append({"type": "text", "text": block["text"]})
            elif kind == "thinking":
                turn.append({"type": "thinking", "thinking": block.get("thinking", ""),
                             "signature": block.get("signature", "")})
            elif kind == "redacted_thinking":
                turn.append({"type": "redacted_thinking", "data": block["data"]})
            else:
                turn.append(block)
        text = "".join(b["text"] for b in turn if b.get("type") == "text").strip()
        usage = raw.get("usage") or {}
        return Reply(
            text=text, turn=turn, stop=raw.get("stop_reason") or "",
            usage={"input": usage.get("input_tokens", 0), "output": usage.get("output_tokens", 0)},
            model=raw.get("model", self.model),
        )


class Gemini(Provider):
    name = "gemini"
    env_key = "GEMINI_API_KEY"
    URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    def request(self, turns, system=None, schema=None):
        contents = []
        for role, content in turns:
            if role == "user":
                contents.append({"role": "user", "parts": [{"text": content}]})
            else:
                contents.append(content)
        config = {"maxOutputTokens": self.cfg.get("max_tokens", 16000)}
        if schema:
            config["responseMimeType"] = "application/json"
        req = {"model": self.model, "contents": contents, "generationConfig": config}
        if system:
            req["systemInstruction"] = {"parts": [{"text": system}]}
        return req

    def send(self, request):
        body = {k: v for k, v in request.items() if k != "model"}
        return post_json(self.URL.format(model=request["model"]), body,
                         {"x-goog-api-key": os.environ[self.env_key]})

    def parse(self, raw):
        candidates = raw.get("candidates") or []
        um = raw.get("usageMetadata") or {}
        usage = {"input": um.get("promptTokenCount", 0),
                 "output": um.get("candidatesTokenCount", 0) + um.get("thoughtsTokenCount", 0)}
        if not candidates:
            stop = (raw.get("promptFeedback") or {}).get("blockReason", "no_candidates")
            return Reply("", {"role": "model", "parts": []}, stop, usage, raw.get("modelVersion", self.model))
        content = dict(candidates[0].get("content") or {"parts": []})
        content.setdefault("role", "model")
        text = "".join(p.get("text", "") for p in content.get("parts", []) if not p.get("thought")).strip()
        return Reply(text, content, candidates[0].get("finishReason", ""), usage,
                     raw.get("modelVersion", self.model))


class OpenAI(Provider):
    name = "openai"
    env_key = "OPENAI_API_KEY"
    URL = "https://api.openai.com/v1/chat/completions"

    def request(self, turns, system=None, schema=None):
        messages = [{"role": "system", "content": system}] if system else []
        messages += [{"role": role, "content": content} for role, content in turns]
        req = {"model": self.model, "messages": messages,
               "max_completion_tokens": self.cfg.get("max_tokens", 16000)}
        if schema:
            req["response_format"] = {"type": "json_object"}
        return req

    def send(self, request):
        return post_json(self.URL, request, {"Authorization": "Bearer " + os.environ[self.env_key]})

    def parse(self, raw):
        choice = (raw.get("choices") or [{}])[0]
        text = ((choice.get("message") or {}).get("content") or "").strip()
        usage = raw.get("usage") or {}
        return Reply(text, text, choice.get("finish_reason", ""),
                     {"input": usage.get("prompt_tokens", 0), "output": usage.get("completion_tokens", 0)},
                     raw.get("model", self.model))


PROVIDERS = {"anthropic": Anthropic, "gemini": Gemini, "openai": OpenAI}


def make(cfg: dict) -> Provider:
    return PROVIDERS[cfg["provider"]](cfg)
