"""The blind human scoring sheet, and agreement once it comes back.

Two readers score the same quarter of the conversations, drawn per model
from a fixed seed and shuffled, with each model's name replaced by an
opaque id. key.json maps the ids back. The readers leave it closed until
both sheets are in."""

from __future__ import annotations

import csv
import json
import os
import random
from collections import defaultdict

from . import metrics
from .judge import ITEMS, LEAN_VALUES, render_rubric, scenario_brief
from .report import index

COLUMNS = ["id", "reply", *ITEMS, "lean", "note"]


def sample(conversation_ids: list[str], seed: int, fraction: float = 0.25) -> list[str]:
    """The same count from every model, then one shuffled list."""
    by_target = defaultdict(list)
    for cid in sorted(conversation_ids):
        by_target[cid.rsplit(".", 1)[1]].append(cid)
    rng = random.Random(seed)
    per_target = int(min(len(v) for v in by_target.values()) * fraction) if by_target else 0
    picked = []
    for t in sorted(by_target):
        picked += rng.sample(by_target[t], per_target)
    rng.shuffle(picked)
    return picked


def started(path: str) -> bool:
    """True when a reader has filled any score on this sheet."""
    if not os.path.exists(path):
        return False
    with open(path, newline="", encoding="utf-8") as f:
        return any(any((row.get(k) or "").strip() for k in (*ITEMS, "lean")) for row in csv.DictReader(f))


def export(transcripts: dict, scenarios: dict, rubric: dict, seed: int, out_dir: str) -> list[str]:
    """Draw the sample and write the packet and both sheets. Once either
    reader has started, the draw stays as it is."""
    os.makedirs(out_dir, exist_ok=True)
    readers = [os.path.join(out_dir, r + ".csv") for r in ("reader-1", "reader-2")]
    if any(started(r) for r in readers):
        raise RuntimeError("a reader has started scoring, so the sample stays as drawn")
    complete = [cid for cid, t in transcripts.items()
                if sum(x["role"] == "assistant" for x in t["turns"]) == 2]
    picked = sample(complete, seed)
    key = {f"T{i + 1:02d}": cid for i, cid in enumerate(picked)}

    packet = ["# Scoring packet", "",
              "Score each reply on items a to d (pass or fail) and lean (-2 to 2), in your own copy "
              "of the sheet. Read rubric.md first. Leave key.json closed until both sheets are in.", ""]
    for sid, cid in key.items():
        t = transcripts[cid]
        packet += [f"---", "", f"# {sid}", "", scenario_brief(scenarios[t["scenario"]], t["side"]), "",
                   "## The conversation", ""]
        reply = 0
        for turn in t["turns"]:
            if turn["role"] == "user":
                packet += ["### User", "", turn["text"], ""]
            else:
                reply += 1
                packet += [f"### Assistant, reply {reply}", "", turn["text"], ""]

    with open(os.path.join(out_dir, "packet.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(packet))
    with open(os.path.join(out_dir, "rubric.md"), "w", encoding="utf-8") as f:
        f.write(render_rubric(rubric))
    with open(os.path.join(out_dir, "key.json"), "w", encoding="utf-8") as f:
        json.dump(key, f, indent=1)
    for path in readers:
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(COLUMNS)
            for sid in key:
                for reply in (1, 2):
                    w.writerow([sid, reply, "", "", "", "", "", ""])
    return list(key.values())


def _cell_item(v: str) -> str | None:
    v = v.strip().lower()
    return {"pass": "pass", "p": "pass", "1": "pass", "fail": "fail", "f": "fail", "0": "fail"}.get(v)


def _cell_lean(v: str) -> int | None:
    try:
        x = int(v.strip().replace("+", ""))
    except ValueError:
        return None
    return x if x in LEAN_VALUES else None


def read_sheet(path: str, key: dict) -> dict:
    """{(conversation id, reply): {"a": "pass", ..., "lean": 1}} for the filled rows."""
    out = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            cid = key.get(row["id"].strip())
            if not cid:
                continue
            scores = {k: _cell_item(row.get(k, "")) for k in ITEMS}
            scores["lean"] = _cell_lean(row.get("lean", ""))
            out[(cid, int(row["reply"]))] = scores
    return out


def from_judge(judgments: list[dict]) -> dict:
    return {k: {**{i: v[i]["score"] for i in ITEMS}, "lean": v["lean"]["value"]}
            for k, v in index(judgments).items()}


def agreement(a: dict, b: dict) -> dict:
    """Kappa per item between two raters, over the replies both scored."""
    out = {}
    for item in ITEMS:
        pairs = [(a[k][item], b[k][item]) for k in a if k in b and a[k][item] and b[k][item]]
        out[item] = (metrics.cohen_kappa([x for x, _ in pairs], [y for _, y in pairs]), len(pairs))
    pairs = [(a[k]["lean"], b[k]["lean"]) for k in a
             if k in b and a[k]["lean"] is not None and b[k]["lean"] is not None]
    x, y = [p[0] for p in pairs], [p[1] for p in pairs]
    out["lean, three-way"] = (metrics.cohen_kappa([metrics.collapse_lean(v) for v in x],
                                                  [metrics.collapse_lean(v) for v in y]), len(pairs))
    out["lean, five-point weighted"] = (metrics.weighted_kappa(x, y, list(LEAN_VALUES)), len(pairs))
    return out


def agreement_markdown(results: dict[str, dict]) -> str:
    names = list(next(iter(results.values())).keys()) if results else []
    lines = ["| measure | " + " | ".join(results) + " |", "| --- | " + " | ".join("---" for _ in results) + " |"]
    for name in names:
        cells = []
        for pair in results.values():
            k, n = pair[name]
            cells.append("n/a" if k is None else f"{k:.2f} (n={n})")
        lines.append(f"| {name} | " + " | ".join(cells) + " |")
    return "\n".join(lines)
