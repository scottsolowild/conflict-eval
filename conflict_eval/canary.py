"""Canaries: replies written to a known score, to check the judges.

Each canary is a first reply to a real telling, built so the rubric's answer
on every item is fixed before a judge reads it. A judge that misses a canary
has read the rubric differently from the way it was written, and the misses
say on which item."""

from __future__ import annotations

import os

from .data import ROOT, load_yaml
from .judge import ITEMS

CANARIES = os.path.join(ROOT, "canaries", "canaries.yaml")


def load(path: str = CANARIES) -> tuple[list[dict], dict]:
    data = load_yaml(path)
    return data["canaries"], data["kinds"]


def transcripts(canaries: list[dict], scenarios: dict) -> list[dict]:
    """Each canary as a one-reply transcript the judge reads like any other."""
    out = []
    for c in canaries:
        telling = scenarios[c["scenario"]]["accounts"][c["side"]]["text"].strip()
        out.append({"id": f"canary.{c['id']}", "scenario": c["scenario"], "side": c["side"],
                    "target": "canary", "note": f"canary, {c['kind']}",
                    "turns": [{"role": "user", "text": telling},
                              {"role": "assistant", "text": c["reply"].strip(), "stop": "written",
                               "model": "canary", "usage": {}}]})
    return out


def score(judgments: list[dict], canaries: list[dict], kinds: dict) -> dict:
    """How often a judge's scores match the ones each canary was built for.
    Returns per-item (hits, n), lean exact and within one, and every miss."""
    by_id = {j["conversation"]: j for j in judgments if j.get("valid") and j.get("reply") == 1}
    hits = {k: 0 for k in ITEMS}
    lean_exact = lean_near = n = 0
    misses = []
    for c in canaries:
        j = by_id.get(f"canary.{c['id']}")
        if not j:
            continue
        n += 1
        want = kinds[c["kind"]]
        got = j["scores"]
        for k in ITEMS:
            if got[k]["score"] == want[k]:
                hits[k] += 1
            else:
                misses.append({"canary": c["id"], "item": k, "want": want[k], "got": got[k]["score"],
                               "reason": got[k]["reason"]})
        g = got["lean"]["value"]
        lean_exact += g == want["lean"]
        lean_near += abs(g - want["lean"]) <= 1
        if g != want["lean"]:
            misses.append({"canary": c["id"], "item": "lean", "want": want["lean"], "got": g,
                           "reason": got["lean"]["reason"]})
    return {"n": n, "items": {k: (hits[k], n) for k in ITEMS},
            "lean_exact": (lean_exact, n), "lean_near": (lean_near, n), "misses": misses}


def markdown(results: dict[str, dict], rubric: dict) -> str:
    names = list(results)
    lines = ["# Canaries", "",
             "Each judge's scores against the scores each canary was written to earn.", "",
             "| item | " + " | ".join(names) + " |", "| --- | " + " | ".join("---" for _ in names) + " |"]
    for k in ITEMS:
        cells = [f"{r['items'][k][0]} of {r['items'][k][1]}" for r in results.values()]
        lines.append(f"| {k}. {rubric['items'][k]['name']} | " + " | ".join(cells) + " |")
    lines.append("| lean, exact | " + " | ".join(f"{r['lean_exact'][0]} of {r['lean_exact'][1]}" for r in results.values()) + " |")
    lines.append("| lean, within one | " + " | ".join(f"{r['lean_near'][0]} of {r['lean_near'][1]}" for r in results.values()) + " |")
    for name, r in results.items():
        if r["misses"]:
            lines += ["", f"## Misses, judge {name}", ""]
            lines += [f"- {m['canary']}, {m['item']}: built for {m['want']}, scored {m['got']}. {m['reason']}"
                      for m in r["misses"]]
    return "\n".join(lines) + "\n"
