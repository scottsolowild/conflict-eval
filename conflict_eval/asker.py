"""The 48-message test: two sets of instructions, read for the person who asked.

The forwarded test reads a reply from the chair of the person it names. This
one reads from the asker's chair. A blind reader holds both sets of
instructions, as Context X and Context Y, writes the reply each would produce
on each of eight messages, read to the letter, and says which serves the
person better. It writes no replies to send, so it costs only the reads.

The method, the prompt, and the 48 messages come from the page at
https://scottsolowild.com/forwarded-test, copied into forwarded/:

- The messages split into six batches of eight, in the page's order.
- Each batch goes to four fresh readers, and every second reader gets the two
  sets of instructions with X and Y swapped, so neither one always reads first.
- A reader answers in JSON. The parse is strict: one JSON object, alone or in
  a single fenced block, with one pair for each of the eight messages. A read
  that does not parse counts as unparsed, never as a verdict.

The reader number is part of the cache namespace, so readers 1 and 3, who
get the same prompt, are two fresh reads.
"""

from __future__ import annotations

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from . import providers
from .cache import Cache
from .data import ROOT, load_yaml

READERS = 4
BATCH = 8
BETTER = ("X", "Y", "same")
X_BLOCK = "=== Context X ===\n{instructions}\n=== End of Context X ==="
Y_BLOCK = "=== Context Y ===\n{instructions}\n=== End of Context Y ==="
MESSAGES = "{the eight messages, numbered}"


def load_messages(root: str = ROOT) -> list[dict]:
    return load_yaml(os.path.join(root, "forwarded", "asker-messages.yaml"))["messages"]


def load_prompt(root: str = ROOT) -> str:
    with open(os.path.join(root, "forwarded", "asker-prompt.md"), encoding="utf-8") as f:
        text = f.read()
    return text.split("\n---\n", 1)[1].strip("\n")


def batches(messages: list[dict]) -> list[list[dict]]:
    return [messages[i:i + BATCH] for i in range(0, len(messages), BATCH)]


def key(names: list[str], reader: int) -> dict[str, str]:
    """Which set of instructions is X and which is Y for one reader. Readers
    1 and 3 read them in the order given, and readers 2 and 4 swapped."""
    first, second = names
    return {"X": first, "Y": second} if reader % 2 else {"X": second, "Y": first}


def render(template: str, x: str, y: str, batch: list[dict]) -> str:
    for anchor in (X_BLOCK, Y_BLOCK, MESSAGES):
        if anchor not in template:
            raise ValueError(f"the 48-message prompt no longer holds {anchor!r}; update asker.py to match")
    numbered = "\n".join(f"{i}. {m['message']}" for i, m in enumerate(batch, 1))
    return (template.replace(X_BLOCK, X_BLOCK.replace("{instructions}", x.strip()))
            .replace(Y_BLOCK, Y_BLOCK.replace("{instructions}", y.strip()))
            .replace(MESSAGES, numbered))


def parse(text: str, count: int) -> dict | None:
    """The read as JSON, or None. One object, alone or inside one fenced
    block, with a pair for each message 1 to count, each pair's better one of
    X, Y, or same, and the dispute counts whole numbers."""
    body = (text or "").strip()
    fence = re.fullmatch(r"```(?:json)?\n(.*)\n```", body, re.S)
    if fence:
        body = fence.group(1).strip()
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("pairs"), list):
        return None
    pairs = data["pairs"]
    if len(pairs) != count or not all(isinstance(p, dict) and type(p.get("message")) is int for p in pairs):
        return None
    if sorted(p["message"] for p in pairs) != list(range(1, count + 1)):
        return None
    for p in pairs:
        if p.get("better") not in BETTER or not isinstance(p.get("why"), str):
            return None
        for side in ("disputed_x", "disputed_y"):
            if side in p and (not isinstance(p[side], int) or isinstance(p[side], bool)):
                return None
    return data


@dataclass
class Result:
    reads: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    pending: int = 0
    pending_in: int = 0


def run(messages: list[dict], contexts: dict[str, str], reader: providers.Provider, cache: Cache,
        live: bool, concurrency: int = 4) -> Result:
    names = list(contexts)
    if len(names) != 2 or any(not contexts[n] for n in names):
        raise ValueError("the 48-message test compares two sets of instructions, each with text")
    template = load_prompt()
    result = Result()
    jobs = [(b, batch, k) for b, batch in enumerate(batches(messages), 1) for k in range(1, READERS + 1)]

    def read(job):
        b, batch, k = job
        sides = key(names, k)
        prompt = render(template, contexts[sides["X"]], contexts[sides["Y"]], batch)
        request = reader.request([("user", prompt)])
        namespace = f"asker/{reader.namespace}/reader-{k}"
        raw = cache.get(namespace, request)
        if raw is None:
            if not live:
                return {"pending": True, "in": len(prompt) // 4}
            try:
                raw = reader.send(request)
            except providers.ProviderError as e:
                return {"error": f"batch {b}, reader {k}: {e}"}
            if not reader.parse(raw).refused:
                cache.put(namespace, request, raw)
        text = reader.parse(raw).text
        data = parse(text, len(batch))
        record = {"batch": b, "reader": k, "key": sides, "text": text, "pairs": None}
        if data:
            side = lambda v: sides[v] if v in sides else v          # noqa: E731
            record["pairs"] = [{"n": batch[p["message"] - 1]["n"], "better": side(p["better"]),
                                "why": p["why"], "felt_managed": p.get("felt_managed"),
                                "disputed": {sides["X"]: p.get("disputed_x"), sides["Y"]: p.get("disputed_y")}}
                               for p in sorted(data["pairs"], key=lambda p: p["message"])]
            record["winner"] = side(data.get("winner"))
            record["misfires"] = data.get("misfires") or []
            record["one_change"] = data.get("one_change")
        return record

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        for r in pool.map(read, jobs):
            if r.get("pending"):
                result.pending += 1
                result.pending_in += r["in"]
            elif r.get("error"):
                result.notes.append(r["error"])
            else:
                result.reads.append(r)
                if r["pairs"] is None:
                    result.notes.append(f"batch {r['batch']}, reader {r['reader']}: the read is not the JSON "
                                        "the prompt asks for, so it counts as unparsed")
    return result


def tally(reads: list[dict], names: list[str]) -> dict:
    """The pairs each set of instructions won, and the ties, overall and by message."""
    total = {n: 0 for n in names} | {"same": 0}
    per = {}
    for r in reads:
        for p in r["pairs"] or []:
            total[p["better"]] += 1
            per.setdefault(p["n"], {n: [] for n in names} | {"same": []})[p["better"]].append(
                p | {"batch": r["batch"], "reader": r["reader"], "key": r["key"]})
    return {"total": total, "per": per, "unparsed": sum(r["pairs"] is None for r in reads)}


def report_markdown(result: Result, messages: list[dict], names: list[str], reader_label: str) -> str:
    first, second = names
    t = tally(result.reads, names)
    pairs = sum(t["total"].values())
    by_n = {m["n"]: m for m in messages}
    lines = [f"# The 48-message test: {first} against {second}", "",
             f"Read by {reader_label}. {len(messages)} messages in {len(batches(messages))} batches, "
             f"{READERS} readers a batch, {len(result.reads)} reads, {pairs} pairs.", "",
             f"| {second} better | {first} better | same |", "| --- | --- | --- |",
             f"| {t['total'][second]} | {t['total'][first]} | {t['total']['same']} |", "",
             f"Reads that are not the JSON the prompt asks for: {t['unparsed']}.", "",
             "## Each message", "", f"Readers who found {second} better, {first} better, and the same.", "",
             "| # | message | " + f"{second} | {first} | same |", "| --- | --- | --- | --- | --- |"]
    for n in sorted(t["per"]):
        row = t["per"][n]
        text = by_n[n]["message"]
        text = text if len(text) <= 70 else text[:67] + "..."
        lines.append(f"| {n} | {text} | {len(row[second])} | {len(row[first])} | {len(row['same'])} |")
    lines += ["", f"## Messages where two or more readers found {first} better", ""]
    lost = [n for n in sorted(t["per"]) if len(t["per"][n][first]) >= 2]
    if not lost:
        lines += ["None.", ""]
    for n in lost:
        lines += [f"### Message {n}", "", f"> {by_n[n]['message']}", ""]
        # A why names X and Y, so each one carries the key its reader had.
        lines += [f"- Batch {p['batch']}, reader {p['reader']} (X = `{p['key']['X']}`, Y = `{p['key']['Y']}`): "
                  f"{p['why']}" for p in t["per"][n][first]] + [""]
    misfires = [(r, m) for r in result.reads for m in (r.get("misfires") or [])]
    lines += ["## Misfires the readers named", ""]
    if not misfires:
        lines += ["None.", ""]
    for r, m in sorted(misfires, key=lambda x: (x[0]["batch"], x[0]["reader"])):
        lines.append(f"- Batch {r['batch']}, reader {r['reader']} (X = `{r['key']['X']}`, Y = `{r['key']['Y']}`): {m}")
    lines += [""]
    if result.notes:
        lines += ["## Notes", ""] + [f"- {n}" for n in result.notes] + [""]
    return "\n".join(lines)


def write(result: Result, messages: list[dict], names: list[str], out_dir: str, reader_label: str) -> str:
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "reads.jsonl"), "w", encoding="utf-8") as f:
        for r in sorted(result.reads, key=lambda r: (r["batch"], r["reader"])):
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    md = report_markdown(result, messages, names, reader_label)
    with open(os.path.join(out_dir, "report.md"), "w", encoding="utf-8") as f:
        f.write(md)
    return md
