"""The command line. ./eval.sh -h lists the commands."""

from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

from . import judge as judging
from . import providers, report, runner, sheet
from .cache import Cache
from .data import ROOT, check_scenario, load_config, load_rubric, load_scenarios

CACHE = os.path.join(ROOT, "cache")
RESULTS = os.path.join(ROOT, "results")
TRANSCRIPTS = os.path.join(RESULTS, "transcripts")
JUDGMENTS = os.path.join(RESULTS, "judgments.jsonl")
EXAMPLES = os.path.join(ROOT, "examples")


def tokens(text: str) -> int:
    """A rough count, four characters to a token. Good enough for a cost range."""
    return max(1, len(text) // 4)


def pick_targets(cfg: dict, only: list[str] | None, quiet: bool = False) -> dict:
    chosen = {}
    for name, tcfg in cfg["targets"].items():
        if only and name not in only:
            continue
        p = providers.make(tcfg)
        why = p.missing()
        if why:
            if not quiet:
                print(f"skip {name}: {why}")
            continue
        chosen[name] = p
    return chosen


def pick_scenarios(only: list[str] | None) -> list[dict]:
    scenarios = load_scenarios()
    return [s for s in scenarios if not only or s["id"] in only]


def cmd_check(args) -> int:
    bad = 0
    for s in load_scenarios():
        problems = check_scenario(s)
        words = {side: len(s["accounts"][side]["text"].split()) for side in "AB"}
        print(f"{s['id']:16} A={words['A']:3} B={words['B']:3} ask={s['ask']:11} "
              + ("ok" if not problems else "PROBLEMS"))
        for p in problems:
            print(f"    {p}")
        bad += bool(problems)
    return 1 if bad else 0


def estimate(cfg: dict, targets: dict, scenarios: list, rubric: dict, cache: Cache) -> dict:
    est = cfg["estimate"]
    pushback = rubric["pushback"]
    convos = runner.run_all(scenarios, targets, cache, pushback, live=False, concurrency=8)
    by_id = {s["id"]: s for s in scenarios}
    rows = {}
    for c in convos:
        row = rows.setdefault(c.target, {"conversations": 0, "calls": 0, "in": 0, "out": 0})
        row["conversations"] += 1
        if not c.calls:
            continue
        telling = tokens(by_id[c.scenario]["accounts"][c.side]["text"])
        second = telling + est["reply_tokens"] + tokens(pushback)
        row["calls"] += c.calls
        row["in"] += (telling + second) if c.calls == 2 else second
        row["out"] += c.calls * est["target_output_tokens"]

    judge = providers.make(cfg["judge"])
    system = tokens(judging.system_prompt(rubric))
    jrow = {"calls": 0, "in": 0, "out": 0}
    transcripts = runner.load_transcripts(TRANSCRIPTS)
    for c in convos:
        brief = tokens(judging.scenario_brief(by_id[c.scenario], c.side))
        convo = tokens(by_id[c.scenario]["accounts"][c.side]["text"]) + tokens(pushback)
        t = transcripts.get(c.id)
        for reply in (1, 2):
            if t and judge.model and len([x for x in t["turns"] if x["role"] == "assistant"]) >= reply:
                if judging.judge_reply(judge, cache, rubric, by_id[c.scenario], t, reply, live=False):
                    continue
            jrow["calls"] += 1
            jrow["in"] += system + brief + convo + reply * est["reply_tokens"]
            jrow["out"] += est["judge_output_tokens"]
    return {"targets": rows, "judge": jrow}


def dollars(row: dict, price: dict, scale: float = 1.0) -> float | None:
    if price.get("input") is None or price.get("output") is None:
        return None
    return (row["in"] * price["input"] + row["out"] * scale * price["output"]) / 1e6


def cmd_plan(args, quiet_header: bool = False) -> dict:
    cfg, rubric = load_config(), load_rubric()
    targets = pick_targets(cfg, args.target)
    scenarios = pick_scenarios(args.scenario)
    e = estimate(cfg, targets, scenarios, rubric, Cache(CACHE))
    lines, total, low, high = [], 0.0, 0.0, 0.0
    lines.append(f"{'':12} {'model':24} {'conversations':>13} {'new calls':>9} {'in tok':>8} {'out tok':>8} {'est. USD':>9}")
    for name, row in e["targets"].items():
        price = cfg["targets"][name]["price"]
        d = dollars(row, price)
        total += d or 0
        low += dollars(row, price, 0.5) or 0
        high += dollars(row, price, 2.0) or 0
        lines.append(f"{name:12} {cfg['targets'][name]['model']:24} {row['conversations']:>13} {row['calls']:>9} "
                     f"{row['in']:>8} {row['out']:>8} {('$%.2f' % d) if d is not None else 'no price':>9}")
    j = e["judge"]
    jd = dollars(j, cfg["judge"]["price"])
    total += jd or 0
    low += dollars(j, cfg["judge"]["price"], 0.5) or 0
    high += dollars(j, cfg["judge"]["price"], 2.0) or 0
    lines.append(f"{'judge':12} {cfg['judge']['model']:24} {'':>13} {j['calls']:>9} {j['in']:>8} {j['out']:>8} "
                 f"{('$%.2f' % jd) if jd is not None else 'no price':>9}")
    print("\n".join(lines))
    calls = sum(r["calls"] for r in e["targets"].values()) + j["calls"]
    print(f"\n{calls} new calls. Estimated ${total:.2f}, between ${low:.2f} and ${high:.2f} "
          f"if thinking runs half or double the assumed length. Cached calls cost nothing.")
    return {"calls": calls, "usd": total}


def cmd_run(args) -> int:
    cfg, rubric = load_config(), load_rubric()
    targets = pick_targets(cfg, args.target)
    scenarios = pick_scenarios(args.scenario)
    cache = Cache(CACHE)
    if args.dry_run:
        out = os.path.join(RESULTS, "dry-run")
        os.makedirs(out, exist_ok=True)
        n = 0
        for s, side, t in runner.conversations(scenarios, targets):
            body = runner.render_dry_run(targets[t], s, side, rubric["pushback"])
            with open(os.path.join(out, f"{s['id']}.{side}.{t}.json"), "w", encoding="utf-8") as f:
                json.dump(body, f, ensure_ascii=False, indent=1)
            n += 1
        with open(os.path.join(out, "judge-system.md"), "w", encoding="utf-8") as f:
            f.write(judging.system_prompt(rubric))
        print(f"Wrote {n} conversations ({2 * n} request bodies) and the judge's system prompt to "
              f"{os.path.relpath(out, ROOT)}/. Nothing was sent.")
        return 0
    plan = cmd_plan(args)
    if plan["calls"] and not args.yes:
        print("\nNothing sent. Run again with --yes to make these calls.")
        return 2
    convos = runner.run_all(scenarios, targets, cache, rubric["pushback"], live=True,
                            concurrency=cfg.get("concurrency", 4))
    n = runner.write_transcripts(convos, TRANSCRIPTS)
    for c in convos:
        if c.note:
            print(f"{c.id}: {c.note}")
    print(f"{n} transcripts in {os.path.relpath(TRANSCRIPTS, ROOT)}/")
    return 0


def cmd_judge(args) -> int:
    cfg, rubric = load_config(), load_rubric()
    judge = providers.make(cfg["judge"])
    scenarios = {s["id"]: s for s in load_scenarios()}
    transcripts = runner.load_transcripts(TRANSCRIPTS)
    if args.target:
        transcripts = {k: v for k, v in transcripts.items() if v["target"] in args.target}
    jobs = [(t, r) for t in transcripts.values()
            for r in range(1, sum(x["role"] == "assistant" for x in t["turns"]) + 1)]
    cache = Cache(CACHE)

    if args.dry_run:
        out = os.path.join(RESULTS, "dry-run", "judge")
        os.makedirs(out, exist_ok=True)
        for t, r in jobs:
            with open(os.path.join(out, f"{t['id']}.reply-{r}.md"), "w", encoding="utf-8") as f:
                f.write(judging.user_prompt(scenarios[t["scenario"]], t, r))
        print(f"Wrote {len(jobs)} judge prompts to {os.path.relpath(out, ROOT)}/. Nothing was sent.")
        return 0

    pending = [(t, r) for t, r in jobs
               if not judging.judge_reply(judge, cache, rubric, scenarios[t["scenario"]], t, r, live=False)]
    if pending:
        why = judge.missing()
        if why:
            print(f"The judge can't run: {why}")
            return 2
        if not args.yes:
            print(f"{len(pending)} replies need a new judge call ({len(jobs) - len(pending)} cached). "
                  "Run again with --yes to make them.")
            return 2

    def score(job):
        t, r = job
        try:
            return judging.judge_reply(judge, cache, rubric, scenarios[t["scenario"]], t, r, live=True)
        except providers.ProviderError as e:
            print(f"{t['id']} reply {r}: {e}")
            return None

    with ThreadPoolExecutor(max_workers=cfg.get("concurrency", 4)) as pool:
        records = [r for r in pool.map(score, jobs) if r]
    report.write_judgments(records, JUDGMENTS)
    health = report.judge_health(records)
    print(f"{len(records)} judgments in {os.path.relpath(JUDGMENTS, ROOT)}. "
          f"Invalid: {health['invalid']}. Quotes not found in the reply: {health['quotes_not_found'] or 'none'}.")
    return 0


def cmd_report(args) -> int:
    cfg, rubric = load_config(), load_rubric()
    scenarios = {s["id"]: s for s in load_scenarios()}
    judgments = report.load_judgments(JUDGMENTS)
    if not judgments:
        print("No judgments yet. Run ./eval.sh judge first.")
        return 2
    targets = sorted({j["target"] for j in judgments})
    nums = report.numbers(judgments, targets, sorted(scenarios))
    health = report.judge_health(judgments)
    transcripts = runner.load_transcripts(TRANSCRIPTS)
    worst = report.worst(judgments)
    os.makedirs(EXAMPLES, exist_ok=True)
    for i, cid in enumerate(worst, 1):
        t = transcripts[cid]
        with open(os.path.join(EXAMPLES, f"worst-{i}.md"), "w", encoding="utf-8") as f:
            f.write(report.worst_markdown(cid, t, scenarios[t["scenario"]], judgments, rubric))
    body = ["# Results", "", report.numbers_markdown(nums, rubric), "",
            f"Judge: {cfg['judge']['model']}. {health['judgments']} judgments, {health['invalid']} invalid, "
            f"quotes not found in the reply: {health['quotes_not_found'] or 'none'}.", "",
            "Worst transcripts: " + ", ".join(f"[{c}](../examples/worst-{i}.md)" for i, c in enumerate(worst, 1)), ""]
    with open(os.path.join(RESULTS, "report.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(body))
    print("\n".join(body))
    return 0


def cmd_sheet(args) -> int:
    cfg, rubric = load_config(), load_rubric()
    scenarios = {s["id"]: s for s in load_scenarios()}
    transcripts = runner.load_transcripts(TRANSCRIPTS)
    out = os.path.join(RESULTS, "sheet")
    picked = sheet.export(transcripts, scenarios, rubric, cfg["seed"], out)
    print(f"{len(picked)} conversations ({2 * len(picked)} replies) in {os.path.relpath(out, ROOT)}/: "
          "packet.md, rubric.md, reader-1.csv, reader-2.csv. key.json stays closed until both sheets are in.")
    return 0


def cmd_agree(args) -> int:
    out = os.path.join(RESULTS, "sheet")
    with open(os.path.join(out, "key.json"), encoding="utf-8") as f:
        key = json.load(f)
    r1 = sheet.read_sheet(os.path.join(out, "reader-1.csv"), key)
    r2 = sheet.read_sheet(os.path.join(out, "reader-2.csv"), key)
    j = sheet.from_judge(report.load_judgments(JUDGMENTS))
    results = {"reader 1 vs reader 2": sheet.agreement(r1, r2),
               "reader 1 vs judge": sheet.agreement(r1, j),
               "reader 2 vs judge": sheet.agreement(r2, j)}
    md = "# Agreement\n\nCohen's kappa, with n the replies both raters scored.\n\n" + sheet.agreement_markdown(results) + "\n"
    with open(os.path.join(RESULTS, "agreement.md"), "w", encoding="utf-8") as f:
        f.write(md)
    print(md)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="conflict_eval")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("check", "plan", "run", "judge", "report", "sheet", "agree"):
        p = sub.add_parser(name)
        if name in ("plan", "run", "judge"):
            p.add_argument("--target", action="append", help="limit to one target (repeatable)")
        if name in ("plan", "run"):
            p.add_argument("--scenario", action="append", help="limit to one scenario id (repeatable)")
        if name in ("run", "judge"):
            p.add_argument("--dry-run", action="store_true", help="render every prompt, call nothing")
            p.add_argument("--yes", action="store_true", help="make the uncached calls")
    args = ap.parse_args(argv)
    handler = {"check": cmd_check, "plan": lambda a: (cmd_plan(a), 0)[1], "run": cmd_run, "judge": cmd_judge,
               "report": cmd_report, "sheet": cmd_sheet, "agree": cmd_agree}[args.cmd]
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
