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


# --- Command-line transports -------------------------------------------------
#
# A subscription CLI in place of an API key. The CLI adds a short block of
# its own (an SDK identity line, the platform, the date, the account email),
# so a CLI run measures the model inside that thin wrapper rather than bare.
# Tools are off, user settings and instruction files stay out, and the
# working folder is an empty one with a plain name.

import shutil
import subprocess
import tempfile
import threading

CLI_FOLDER = os.path.join(tempfile.gettempdir(), "chat")
CLI_ENV_KEEP = ("HOME", "PATH", "USER", "LANG", "TMPDIR")


def _cli_env() -> dict:
    return {k: os.environ[k] for k in CLI_ENV_KEEP if k in os.environ}


class ClaudeCLI(Provider):
    """Claude through the `claude` CLI on the logged-in subscription. A whole
    conversation runs in one process over stream-json, so turn 2 continues
    turn 1 the way a chat does."""
    name = "claude-cli"
    env_key = ""
    timeout = 900

    def missing(self):
        if not self.model:
            return "no model id in config.yaml"
        if not shutil.which("claude"):
            return "the claude CLI is not installed"
        return None

    def _base(self, system: str) -> list[str]:
        cmd = ["claude", "-p", "--model", self.model, "--tools", "", "--setting-sources", "",
               "--strict-mcp-config", "--no-session-persistence", "--system-prompt", system]
        if self.cfg.get("effort"):
            cmd += ["--effort", self.cfg["effort"]]
        return cmd

    # Targets: one process per conversation.
    def conversation_request(self, user_turns: list[str]) -> dict:
        return {"cli": "claude", "model": self.model, "effort": self.cfg.get("effort"),
                "system": self.cfg.get("system", ""), "user_turns": user_turns}

    def converse(self, request: dict) -> dict:
        os.makedirs(CLI_FOLDER, exist_ok=True)
        cmd = self._base(request["system"]) + ["--input-format", "stream-json",
                                               "--output-format", "stream-json", "--verbose"]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, env=_cli_env(), cwd=CLI_FOLDER)
        timer = threading.Timer(self.timeout, proc.kill)
        timer.start()
        results = []
        try:
            for text in request["user_turns"]:
                proc.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n")
                proc.stdin.flush()
                for line in proc.stdout:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if event.get("type") == "result":
                        results.append(event)
                        break
                else:
                    break
                if results[-1].get("is_error"):
                    break
            proc.stdin.close()
            proc.wait(timeout=60)
        finally:
            timer.cancel()
        if not results:
            raise ProviderError(f"claude CLI gave no result (exit {proc.returncode}): {proc.stderr.read()[:400]}")
        return {"results": results}

    def parse_conversation(self, raw: dict) -> list[Reply]:
        return [self._reply(ev) for ev in raw["results"]]

    def _reply(self, ev: dict) -> Reply:
        text = (ev.get("result") or "").strip()
        usage = ev.get("usage") or {}
        models = list((ev.get("modelUsage") or {}).keys())
        stop = "error" if ev.get("is_error") else (ev.get("subtype") or "")
        return Reply(text, text, stop,
                     {"input": usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0)
                      + usage.get("cache_creation_input_tokens", 0), "output": usage.get("output_tokens", 0)},
                     models[0] if models else self.model)

    # The judge: one turn, a system prompt, and a JSON schema.
    def request(self, turns, system=None, schema=None):
        return {"cli": "claude", "model": self.model, "effort": self.cfg.get("effort"),
                "system": system or "", "schema": schema, "prompt": turns[-1][1]}

    def send(self, request):
        os.makedirs(CLI_FOLDER, exist_ok=True)
        cmd = self._base(request["system"]) + ["--output-format", "json"]
        if request.get("schema"):
            cmd += ["--json-schema", json.dumps(request["schema"])]
        try:
            done = subprocess.run(cmd, input=request["prompt"], capture_output=True, text=True,
                                  env=_cli_env(), cwd=CLI_FOLDER, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            raise ProviderError("claude CLI timed out") from None
        try:
            return json.loads(done.stdout)
        except json.JSONDecodeError:
            raise ProviderError(f"claude CLI exit {done.returncode}: {(done.stderr or done.stdout)[:400]}") from None

    def parse(self, raw):
        reply = self._reply(raw)
        structured = raw.get("structured_output")
        if structured is not None:
            reply.text = json.dumps(structured)
        return reply


PROVIDERS["claude-cli"] = ClaudeCLI


class GeminiCLI(Provider):
    """Gemini through the `gemini` CLI on the logged-in Google account. Each
    conversation gets its own empty folder, so turn 2 resumes the folder's
    latest session and nothing else. GEMINI_SYSTEM_MD replaces the CLI's
    built-in instructions with the file this run writes."""
    name = "gemini-cli"
    env_key = ""
    timeout = 900

    def missing(self):
        if not self.model:
            return "no model id in config.yaml"
        if not shutil.which("gemini"):
            return "the gemini CLI is not installed (npm install -g @google/gemini-cli)"
        if not os.path.exists(os.path.expanduser("~/.gemini/oauth_creds.json")):
            return "the gemini CLI is not signed in (run gemini once and choose Sign in with Google)"
        return None

    def _run(self, folder: str, prompt: str, system: str, resume: bool) -> dict:
        os.makedirs(folder, exist_ok=True)
        system_file = os.path.join(os.path.dirname(folder), "system.md")
        with open(system_file, "w", encoding="utf-8") as f:
            f.write(system or " ")
        env = {**_cli_env(), "GEMINI_SYSTEM_MD": system_file}
        cmd = ["gemini", "-m", self.model, "-o", "json", "--approval-mode", "plan", "--skip-trust",
               "-e", "none", "-p", prompt]
        if resume:
            cmd += ["--resume", "latest"]
        try:
            done = subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=folder, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            raise ProviderError("gemini CLI timed out") from None
        try:
            out = json.loads(done.stdout[done.stdout.index("{"):])
        except (ValueError, json.JSONDecodeError):
            raise ProviderError(f"gemini CLI exit {done.returncode}: {(done.stderr or done.stdout)[:400]}") from None
        if out.get("error"):
            raise ProviderError(f"gemini CLI: {str(out['error'])[:400]}")
        return out

    def conversation_request(self, user_turns: list[str]) -> dict:
        return {"cli": "gemini", "model": self.model, "system": self.cfg.get("system", ""),
                "user_turns": user_turns}

    def converse(self, request: dict) -> dict:
        from .cache import request_key
        base = os.path.join(tempfile.gettempdir(), "gemini-runs", request_key(request)[:16])
        folder = os.path.join(base, "chat")
        shutil.rmtree(base, ignore_errors=True)
        results = []
        for i, text in enumerate(request["user_turns"]):
            results.append(self._run(folder, text, request["system"], resume=i > 0))
            if not (results[-1].get("response") or "").strip():
                break
        shutil.rmtree(base, ignore_errors=True)
        return {"results": results}

    def parse_conversation(self, raw: dict) -> list[Reply]:
        return [self._reply(r) for r in raw["results"]]

    def _reply(self, out: dict) -> Reply:
        text = (out.get("response") or "").strip()
        models = (out.get("stats") or {}).get("models") or {}
        name = next(iter(models), self.model)
        tokens = (models.get(name) or {}).get("tokens") or {}
        return Reply(text, text, "stop" if text else "empty",
                     {"input": tokens.get("prompt", 0), "output": tokens.get("candidates", 0) + tokens.get("thoughts", 0)},
                     name)

    def request(self, turns, system=None, schema=None):
        return {"cli": "gemini", "model": self.model, "system": system or "", "prompt": turns[-1][1]}

    def send(self, request):
        from .cache import request_key
        base = os.path.join(tempfile.gettempdir(), "gemini-runs", request_key(request)[:16])
        try:
            return self._run(os.path.join(base, "chat"), request["prompt"], request["system"], resume=False)
        finally:
            shutil.rmtree(base, ignore_errors=True)

    def parse(self, raw):
        return self._reply(raw)


PROVIDERS["gemini-cli"] = GeminiCLI
