"""Run each telling in its own fresh conversation: turn 1 is the telling,
turn 2 is the pushback. Every call goes through the cache."""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from . import providers
from .cache import Cache
from .data import SIDES

PLACEHOLDER = "<the model's first reply goes here>"


@dataclass
class Conversation:
    scenario: str
    side: str
    target: str
    turns: list = field(default_factory=list)       # [("user", text), ("assistant", native), ...]
    replies: list = field(default_factory=list)     # providers.Reply, one per assistant turn
    calls: int = 0                                  # uncached calls made, or that a run would make
    note: str = ""

    @property
    def id(self) -> str:
        return f"{self.scenario}.{self.side}.{self.target}"


def conversations(scenarios: list, targets: dict) -> list[tuple[dict, str, str]]:
    """Every (scenario, side, target). A scenario may carry "_sides" to run
    one side alone, as the smoke test does."""
    return [(s, side, t) for t in targets for s in scenarios for side in s.get("_sides", SIDES)]


def run_one(provider: providers.Provider, cache: Cache, scenario: dict, side: str, target: str,
            pushback: str, live: bool) -> Conversation:
    """Play one conversation. With live=False nothing is sent: a cached turn is
    read back, and an uncached turn is counted and left empty."""
    convo = Conversation(scenario["id"], side, target)
    user_turns = [scenario["accounts"][side]["text"].strip(), pushback]
    if hasattr(provider, "converse"):
        # A CLI runs the whole conversation in one process, so it caches whole.
        request = provider.conversation_request(user_turns)
        raw = cache.get(provider.namespace, request)
        if raw is None:
            if not live:
                convo.calls = len(user_turns)
                convo.note = "not run"
                return convo
            raw = provider.converse(request)
            if len(raw.get("results", [])) == len(user_turns):
                cache.put(provider.namespace, request, raw)
            convo.calls = len(user_turns)
        for user_text, reply in zip(user_turns, provider.parse_conversation(raw)):
            convo.turns += [("user", user_text), ("assistant", reply.turn)]
            convo.replies.append(reply)
            if reply.refused:
                convo.note = f"no answer on turn {len(convo.replies)} (stop: {reply.stop})"
                break
        return convo
    for user_text in user_turns:
        convo.turns.append(("user", user_text))
        request = provider.request(convo.turns)
        raw = cache.get(provider.namespace, request)
        if raw is None:
            convo.calls += 1
            if not live:
                # The second turn can't be keyed until the first one exists.
                convo.calls += len(user_turns) - len(convo.replies) - 1
                convo.note = "not run"
                return convo
            raw = provider.send(request)
            cache.put(provider.namespace, request, raw)
        reply = provider.parse(raw)
        convo.replies.append(reply)
        convo.turns.append(("assistant", reply.turn))
        if reply.refused:
            convo.note = f"no answer on turn {len(convo.replies)} (stop: {reply.stop})"
            return convo
    return convo


def render_dry_run(provider: providers.Provider, scenario: dict, side: str, pushback: str) -> dict:
    """The two request bodies a conversation sends, with the first reply left
    as a placeholder."""
    first = [("user", scenario["accounts"][side]["text"].strip())]
    if hasattr(provider, "converse"):
        return {"conversation": provider.conversation_request([first[0][1], pushback])}
    stand_in = provider.parse(_stand_in_raw(provider, PLACEHOLDER)).turn
    second = first + [("assistant", stand_in), ("user", pushback)]
    return {"turn_1": provider.request(first), "turn_2": provider.request(second)}


def _stand_in_raw(provider: providers.Provider, text: str) -> dict:
    if provider.name == "anthropic":
        return {"content": [{"type": "text", "text": text}], "stop_reason": "end_turn"}
    if provider.name == "gemini":
        return {"candidates": [{"content": {"role": "model", "parts": [{"text": text}]}}]}
    if provider.name == "openai":
        return {"choices": [{"message": {"content": text}}]}
    return provider.stand_in(text)


def run_all(scenarios: list, targets: dict, cache: Cache, pushback: str, live: bool,
            concurrency: int = 4) -> list[Conversation]:
    jobs = conversations(scenarios, targets)

    def play(job):
        s, side, t = job
        try:
            return run_one(targets[t], cache, s, side, t, pushback, live)
        except providers.ProviderError as e:
            # One failed conversation leaves the rest running. A rerun picks
            # it up, since every turn that did finish is cached.
            return Conversation(s["id"], side, t, note=f"error: {e}")

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        return list(pool.map(play, jobs))


def transcript(convo: Conversation) -> dict:
    """A conversation as plain text, the form the judge and the readers see."""
    turns = []
    replies = iter(convo.replies)
    for role, content in convo.turns:
        if role == "user":
            turns.append({"role": "user", "text": content})
        else:
            r = next(replies)
            turns.append({"role": "assistant", "text": r.text, "stop": r.stop,
                          "model": r.model, "usage": r.usage})
    return {"id": convo.id, "scenario": convo.scenario, "side": convo.side,
            "target": convo.target, "note": convo.note, "turns": turns}


def write_transcripts(convos: list[Conversation], out_dir: str) -> int:
    os.makedirs(out_dir, exist_ok=True)
    n = 0
    for c in convos:
        if not c.replies:
            continue
        with open(os.path.join(out_dir, c.id + ".json"), "w", encoding="utf-8") as f:
            json.dump(transcript(c), f, ensure_ascii=False, indent=1)
        n += 1
    return n


def load_transcripts(out_dir: str) -> dict[str, dict]:
    if not os.path.isdir(out_dir):
        return {}
    out = {}
    for name in sorted(os.listdir(out_dir)):
        if name.endswith(".json"):
            with open(os.path.join(out_dir, name), encoding="utf-8") as f:
                t = json.load(f)
            out[t["id"]] = t
    return out
