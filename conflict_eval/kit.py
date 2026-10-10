"""The app arm. People meet these models through apps with their own
instructions on top, so a person pastes a few tellings into an app by hand
and pastes the replies back into the kit page. `build` renders that page
from the scenario files. `load` turns the saved replies into transcripts the
judge scores like any other target.

Nothing here drives an app. The replies arrive by a person's hand."""

from __future__ import annotations

import json
import os
import re

KIT_TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kit_template.html")


def conversations(scenarios: list[dict], chosen: list[str]) -> list[dict]:
    by_id = {s["id"]: s for s in scenarios}
    out = []
    for sid in chosen:
        s = by_id[sid]
        for side in ("A", "B"):
            cid = f"{sid}.{side}"
            out.append({
                "id": cid,
                "dom": re.sub(r"[^A-Za-z0-9_-]", "-", cid),
                "scenario": sid,
                "side": side,
                "speaker": s["people"][side]["name"],
                "telling": s["accounts"][side]["text"].strip(),
            })
    return out


def build(scenarios: list[dict], chosen: list[str], pushback: str, out_path: str) -> int:
    convos = conversations(scenarios, chosen)
    data = json.dumps({"pushback": pushback, "conversations": convos}, ensure_ascii=False)
    data = data.replace("</", "<\\/")
    with open(KIT_TEMPLATE, encoding="utf-8") as f:
        page = f.read().replace("/*__KIT_DATA__*/", data)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(page)
    return len(convos)


def _rows(raw) -> dict[str, dict]:
    """Accept the store's rows as {id: body}, a list of {id, ...} or of
    {id, data: {...}}, the shapes a store export comes in."""
    if isinstance(raw, dict) and "documents" in raw:
        raw = raw["documents"]
    if isinstance(raw, dict):
        return {k: v for k, v in raw.items() if isinstance(v, dict)}
    rows = {}
    for item in raw:
        body = item.get("data", item)
        cid = item.get("id") or item.get("doc_id") or str(item.get("path", "")).rsplit("/", 1)[-1]
        rows[cid] = body
    return rows


def _read(path: str) -> dict[str, dict]:
    """One JSON file of rows, or a folder of one file per document, named
    for the document id (the shape a store read with out_dir writes)."""
    if os.path.isdir(path):
        rows = {}
        for root, _, names in os.walk(path):
            for name in names:
                if name.endswith(".json"):
                    with open(os.path.join(root, name), encoding="utf-8") as f:
                        obj = json.load(f)
                    body = obj if "reply1" in obj else obj.get("data", obj)
                    rows[name[:-5]] = body
        return rows
    with open(path, encoding="utf-8") as f:
        return _rows(json.load(f))


def load(path: str, scenarios: list[dict], target: str, pushback: str,
         model: str | None = None) -> list[dict]:
    """Transcripts for every conversation with both replies pasted."""
    rows = _read(path)
    by_id = {s["id"]: s for s in scenarios}
    out = []
    for cid, body in sorted(rows.items()):
        if "." not in cid:
            continue
        sid, side = cid.split(".", 1)
        r1, r2 = (body.get("reply1") or "").strip(), (body.get("reply2") or "").strip()
        if sid not in by_id or side not in ("A", "B") or not (r1 and r2):
            continue
        shown = " ".join(x for x in (body.get("app"), body.get("model_shown")) if x)
        model = (shown if body.get("model_shown") else None) or model or shown or target
        turns = [
            {"role": "user", "text": by_id[sid]["accounts"][side]["text"].strip()},
            {"role": "assistant", "text": r1, "stop": "pasted", "model": model, "usage": {}},
            {"role": "user", "text": pushback},
            {"role": "assistant", "text": r2, "stop": "pasted", "model": model, "usage": {}},
        ]
        out.append({"id": f"{cid}.{target}", "scenario": sid, "side": side, "target": target,
                    "note": "pasted by hand from the app", "turns": turns})
    return out
