"""The forwarded test: a reply about someone, read by that someone.

Advice about another person gets forwarded, screenshotted, or read aloud to
the person it is about, and most evals read it only from the asker's chair.
Here a second model plays the person the message names and reads each
version of the reply as if it had been forwarded to them, word for word.

The method, the prompt, and the 36 messages come from the page at
https://scottsolowild.com/forwarded-test, copied into forwarded/:

- Each context is a set of instructions under test, or none at all. A reply
  runs in a fresh session whose system prompt is one line, "You are a helpful
  AI assistant.", followed by the context.
- Each message gets two sets of replies under every context.
- Each set goes to two fresh readers. The first reads the versions in a
  seeded shuffle and the second in the reverse order, so every version gets
  four reads per message, and no version always reads first.
- A reader ends on one VERDICT line. The parse is strict, and a read whose
  last line does not parse counts as unparsed, never as a verdict.

Every call goes through the cache. A reply's set number and a read's reader
number are part of the cache namespace, since the requests are otherwise
identical and each set is a fresh sample.
"""

from __future__ import annotations

import json
import os
import random
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from . import providers
from .cache import Cache
from .data import ROOT, load_yaml

DIR = os.path.join(ROOT, "forwarded")
VERDICTS = ("yes", "mixed", "no")
LETTERS = "ABCDEFGH"
READERS = 2
NUMBER_WORDS = {2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight"}

# The page writes the prompt for three versions. These are the lines that
# name the count, in the page's own words; render() checks each is present.
VERSIONS_LINE = "three versions of the reply, A, B, and C"
RANK_LINE = "rank A, B, and C"
VERDICT_LINE = "VERDICT A=<yes|mixed|no> B=<yes|mixed|no> C=<yes|mixed|no> RANK=<for example B>A=C>"
BLOCKS = "=== Version A ===\n{reply}\n=== End of version A ===\n(and so on for B and C)"


def load_messages(root: str = ROOT) -> list[dict]:
    return load_yaml(os.path.join(root, "forwarded", "messages.yaml"))["messages"]


def load_prompt(root: str = ROOT) -> str:
    """The reader's prompt, with the source note above the line taken off."""
    with open(os.path.join(root, "forwarded", "prompt.md"), encoding="utf-8") as f:
        text = f.read()
    return text.split("\n---\n", 1)[1].strip("\n")


def resolve_contexts(specs: list[str], configured: dict, root: str = ROOT) -> dict[str, str | None]:
    """Each spec is "none", a name under forwarded.contexts, or NAME=PATH.
    Returns name -> context text, with None for no context."""
    out = {}
    for spec in specs:
        if "=" in spec:
            name, path = spec.split("=", 1)
        elif spec in configured:
            name, path = spec, configured[spec]
        elif spec == "none":
            name, path = spec, None
        else:
            raise ValueError(f"unknown context {spec!r}: name one in config.yaml, 'none', or NAME=PATH")
        if name in out:
            raise ValueError(f"context {name!r} named twice")
        if path is None:
            out[name] = None
            continue
        full = path if os.path.isabs(path) else os.path.join(root, path)
        with open(full, encoding="utf-8") as f:
            out[name] = f.read().strip()
    if len(out) < 2:
        raise ValueError("the forwarded test compares two or more contexts")
    if len(out) > len(LETTERS):
        raise ValueError(f"at most {len(LETTERS)} contexts fit in one read")
    return out


def system_prompt(base: str, context: str | None) -> str:
    return base if not context else f"{base}\n\n{context}"


def orders(names: list[str], seed, message: int, set_n: int) -> list[list[str]]:
    """The version order for each reader of one set: a seeded shuffle, then its reverse."""
    first = list(names)
    random.Random(f"{seed}:{message}:{set_n}").shuffle(first)
    return [first, first[::-1]]


def listing(letters: str) -> str:
    if len(letters) == 2:
        return f"{letters[0]} and {letters[1]}"
    return ", ".join(letters[:-1]) + f", and {letters[-1]}"


def render(template: str, reader: str, message: str, replies: list[str]) -> str:
    """Fill the page's prompt for one read, with the replies in reading order."""
    for anchor in ("{the other person}", "{the message}", VERSIONS_LINE, RANK_LINE, VERDICT_LINE, BLOCKS):
        if anchor not in template:
            raise ValueError(f"the forwarded prompt no longer holds {anchor!r}; update forwarded.py to match")
    letters = LETTERS[:len(replies)]
    word = NUMBER_WORDS[len(replies)]
    example = f"{letters[1]}>{letters[0]}" + "".join(f"={x}" for x in letters[2:])
    verdict = "VERDICT " + " ".join(f"{x}=<yes|mixed|no>" for x in letters) + f" RANK=<for example {example}>"
    blocks = "\n".join(f"=== Version {x} ===\n{text.strip()}\n=== End of version {x} ==="
                       for x, text in zip(letters, replies))
    return (template.replace("{the other person}", reader)
            .replace("{the message}", message)
            .replace(VERSIONS_LINE, f"{word} versions of the reply, {listing(letters)}")
            .replace(RANK_LINE, f"rank {listing(letters)}")
            .replace(VERDICT_LINE, verdict)
            .replace(BLOCKS, blocks))


def parse_verdict(text: str, letters: str) -> dict | None:
    """The last non-empty line, read strictly: every letter once, in order, then
    a rank that names every letter once. Anything else is None."""
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return None
    pattern = ("VERDICT " + " ".join(f"{x}=(yes|mixed|no)" for x in letters) + r" RANK=(\S+)")
    m = re.fullmatch(pattern, lines[-1])
    if not m:
        return None
    rank = m.group(len(letters) + 1)
    named = re.split(r"[>=]", rank)
    if sorted(named) != sorted(letters):
        return None
    return {"verdicts": dict(zip(letters, m.groups()[:len(letters)])), "rank": rank}


@dataclass
class Plan:
    replies: int = 0          # uncached reply calls
    reads: int = 0            # uncached read calls, keyed or not
    reply_in: int = 0
    read_in: int = 0


@dataclass
class Result:
    replies: list = field(default_factory=list)   # one dict per reply
    reads: list = field(default_factory=list)     # one dict per read
    notes: list = field(default_factory=list)
    plan: Plan = field(default_factory=Plan)


def tokens(text: str) -> int:
    return max(1, len(text) // 4)


def run(messages: list[dict], contexts: dict[str, str | None], target: providers.Provider,
        reader: providers.Provider, cache: Cache, cfg: dict, live: bool,
        concurrency: int = 4, reply_tokens: int = 700) -> Result:
    """Make or read back every reply, then every read. With live=False nothing
    is sent, and the plan counts what a live run would send."""
    sets, seed, base = cfg.get("sets", 2), cfg["seed"], cfg["system"]
    names = list(contexts)
    result = Result()
    template = load_prompt()

    def reply_job(job):
        m, name, s = job
        system = system_prompt(base, contexts[name])
        request = target.request([("user", m["message"])], system=system)
        namespace = f"forwarded/{target.namespace}/set-{s}"
        raw = cache.get(namespace, request)
        if raw is None:
            if not live:
                return {"pending": True, "in": tokens(system + m["message"])}
            try:
                raw = target.send(request)
            except providers.ProviderError as e:
                return {"error": f"message {m['n']}, {name}, set {s}: {e}"}
            reply = target.parse(raw)
            if not reply.refused:
                cache.put(namespace, request, raw)
        reply = target.parse(raw)
        return {"message": m["n"], "context": name, "set": s, "text": reply.text,
                "stop": reply.stop, "model": reply.model, "refused": reply.refused}

    jobs = [(m, name, s) for m in messages for name in names for s in range(1, sets + 1)]
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        made = list(pool.map(reply_job, jobs))
    have = {}
    for (m, name, s), r in zip(jobs, made):
        if r.get("pending"):
            result.plan.replies += 1
            result.plan.reply_in += r["in"]
        elif r.get("error"):
            result.notes.append(r["error"])
        else:
            result.replies.append(r)
            if r["refused"]:
                result.notes.append(f"message {m['n']}, {name}, set {s}: no reply (stop: {r['stop']})")
            else:
                have[(m["n"], name, s)] = r["text"]

    def read_job(job):
        m, s, k, order = job
        texts = [have.get((m["n"], name, s)) for name in order]
        if any(t is None for t in texts):
            # A read waits on its replies, so it can't be keyed yet.
            return {"pending": True, "in": tokens(template) + len(order) * reply_tokens}
        prompt = render(template, m["reader"], m["message"], texts)
        request = reader.request([("user", prompt)])
        namespace = f"forwarded/{reader.namespace}/reader-{k}"
        raw = cache.get(namespace, request)
        if raw is None:
            if not live:
                return {"pending": True, "in": tokens(prompt)}
            try:
                raw = reader.send(request)
            except providers.ProviderError as e:
                return {"error": f"read of message {m['n']}, set {s}, reader {k}: {e}"}
            if not reader.parse(raw).refused:
                cache.put(namespace, request, raw)
        text = reader.parse(raw).text
        letters = LETTERS[:len(order)]
        parsed = parse_verdict(text, letters)
        key = dict(zip(letters, order))
        return {"message": m["n"], "set": s, "reader": k, "key": key,
                "verdicts": {key[x]: v for x, v in parsed["verdicts"].items()} if parsed else None,
                "rank": parsed["rank"] if parsed else None, "text": text}

    read_jobs = [(m, s, k, order) for m in messages for s in range(1, sets + 1)
                 for k, order in enumerate(orders(names, seed, m["n"], s), 1)]
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        read = list(pool.map(read_job, read_jobs))
    for r in read:
        if r.get("pending"):
            result.plan.reads += 1
            result.plan.read_in += r["in"]
        elif r.get("error"):
            result.notes.append(r["error"])
        else:
            result.reads.append(r)
            if r["verdicts"] is None:
                result.notes.append(f"read of message {r['message']}, set {r['set']}, reader {r['reader']}: "
                                    "its last line is not a VERDICT line, so it counts as unparsed")
    return result


def tally(reads: list[dict], names: list[str]) -> dict:
    """Per context, the reads that landed, landed in part, and missed, and the
    reads that gave no verdict."""
    out = {name: {v: 0 for v in VERDICTS} | {"reads": 0} for name in names}
    unparsed = 0
    for r in reads:
        if r["verdicts"] is None:
            unparsed += 1
            continue
        for name, v in r["verdicts"].items():
            out[name][v] += 1
            out[name]["reads"] += 1
    return {"contexts": out, "unparsed": unparsed}


def by_message(reads: list[dict], names: list[str]) -> dict[int, dict]:
    rows = {}
    for r in reads:
        row = rows.setdefault(r["message"], {name: {v: 0 for v in VERDICTS} for name in names})
        for name, v in (r["verdicts"] or {}).items():
            row[name][v] += 1
    return rows


def report_markdown(result: Result, messages: list[dict], names: list[str], target_label: str,
                    reader_label: str) -> str:
    t = tally(result.reads, names)
    by_n = {m["n"]: m for m in messages}
    lines = [f"# The forwarded test: {', '.join(names)}", "",
             f"Replies by {target_label}, read by {reader_label}. {len(messages)} of 36 messages, "
             f"{len(result.reads)} reads, each reading every version once.", "",
             "| context | landed | in part | missed | reads |", "| --- | --- | --- | --- | --- |"]
    for name in names:
        c = t["contexts"][name]
        lines.append(f"| {name} | {c['yes']} | {c['mixed']} | {c['no']} | {c['reads']} |")
    lines += ["", f"Reads with no VERDICT line: {t['unparsed']}.", ""]
    rows = by_message(result.reads, names)
    lines += ["## Each message", "", "Landed, in part, and missed, per context.", "",
              "| # | reader plays | " + " | ".join(names) + " |",
              "| --- | --- | " + " | ".join("---" for _ in names) + " |"]
    for n in sorted(rows):
        cells = [f"{rows[n][name]['yes']}/{rows[n][name]['mixed']}/{rows[n][name]['no']}" for name in names]
        lines.append(f"| {n} | {by_n[n]['reader']} | " + " | ".join(cells) + " |")
    missed = [r for r in result.reads if r["verdicts"] and "no" in r["verdicts"].values()]
    lines += ["", "## The reads with a miss", ""]
    if not missed:
        lines += ["None.", ""]
    for r in missed:
        m = by_n[r["message"]]
        key = ", ".join(f"{x} = `{name}`" for x, name in r["key"].items())
        lines += [f"### Message {r['message']}, set {r['set']}, reader {r['reader']}", "",
                  f"> {m['message']}", "", f"Reader plays {m['reader']}. Versions: {key}. "
                  f"Missed under: {', '.join(f'`{n}`' for n, v in r['verdicts'].items() if v == 'no')}.", "",
                  "The read, word for word:", "", "```", r["text"].strip(), "```", ""]
    if result.notes:
        lines += ["## Notes", ""] + [f"- {n}" for n in result.notes] + [""]
    return "\n".join(lines)


def write(result: Result, messages: list[dict], names: list[str], out_dir: str,
          target_label: str, reader_label: str) -> str:
    os.makedirs(out_dir, exist_ok=True)
    for kind in ("replies", "reads"):
        with open(os.path.join(out_dir, f"{kind}.jsonl"), "w", encoding="utf-8") as f:
            for row in sorted(getattr(result, kind), key=lambda r: (r["message"], r["set"], r.get("context", ""), r.get("reader", 0))):
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
    md = report_markdown(result, messages, names, target_label, reader_label)
    with open(os.path.join(out_dir, "report.md"), "w", encoding="utf-8") as f:
        f.write(md)
    return md
