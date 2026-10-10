"""Load the scenarios, the rubric, and the config, and check each scenario
against the rules in scenarios/README.md."""

from __future__ import annotations

import glob
import os

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIDES = ("A", "B")
ASKS = ("open", "am-i-wrong", "practical")


def load_yaml(path: str):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_config(root: str = ROOT) -> dict:
    return load_yaml(os.path.join(root, "config.yaml"))


def load_rubric(root: str = ROOT) -> dict:
    return load_yaml(os.path.join(root, "rubric.yaml"))


def load_scenarios(root: str = ROOT) -> list[dict]:
    paths = sorted(glob.glob(os.path.join(root, "scenarios", "*.yaml")))
    return [load_yaml(p) for p in paths]


def other(side: str) -> str:
    return "B" if side == "A" else "A"


def check_scenario(s: dict) -> list[str]:
    """Return the rules this scenario breaks, as readable lines."""
    problems = []
    ids = {f["id"] for f in s["facts"]}
    words = {}
    for side in SIDES:
        a = s["accounts"][side]
        words[side] = len(a["text"].split())
        unknown = [i for i in a["leaves_out"] if i not in ids]
        if unknown:
            problems.append(f"{side}: leaves_out names unknown facts {unknown}")
        if len(a["leaves_out"]) < 2:
            problems.append(f"{side}: leaves out fewer than two facts")
        if len(a["one_sided"]) < 2:
            problems.append(f"{side}: fewer than two one-sided words")
        if not a["motive"]:
            problems.append(f"{side}: supplies no motive for the other person")
        text = a["text"].lower()
        missing = [w for w in a["one_sided"] + a["motive"] if w.lower() not in text]
        if missing:
            problems.append(f"{side}: quoted words not in the telling {missing}")
        if not a["cannot_support"] or not a["can_support"]:
            problems.append(f"{side}: cannot_support and can_support both need entries")
    ratio = max(words.values()) / min(words.values())
    if ratio > 1.10:
        problems.append(f"tellings differ in length by {ratio - 1:.0%} (A={words['A']}, B={words['B']})")
    if s.get("ask") not in ASKS:
        problems.append(f"ask is {s.get('ask')!r}, expected one of {ASKS}")
    for side in SIDES:
        if not s["own_words"].get(side):
            problems.append(f"own_words is missing {side}")
    return problems
