"""The command line. ./eval.sh -h lists the commands."""

from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

from . import judge as judging
from . import kit, providers, report, runner, sheet
from .cache import Cache
from .data import ROOT, check_scenario, load_config, load_rubric, load_scenarios

CACHE = os.path.join(ROOT, "cache")
RESULTS = os.path.join(ROOT, "results")
TRANSCRIPTS = os.path.join(RESULTS, "transcripts")
JUDGMENTS = os.path.join(RESULTS, "judgments")
EXAMPLES = os.path.join(ROOT, "examples")
KIT_PAGE = os.path.join(RESULTS, "kit", "index.html")


def judgments_path(name: str) -> str:
    return os.path.join(JUDGMENTS, name + ".jsonl")


def tokens(text: str) -> int:
    """A rough count, four characters to a token. Good enough for a cost range."""
    return max(1, len(text) // 4)


def pick(section: dict, only: list[str] | None, quiet: bool = False) -> dict:
    """The entries of a config section whose provider can run, by name."""
    chosen = {}
    for name, cfg in section.items():
        if only and name not in only:
            continue
        if cfg.get("disabled") and not only:
            continue
        p = providers.make(cfg)
        why = p.missing()
        if why:
            if not quiet:
                print(f"skip {name}: {why}")
            continue
        chosen[name] = p
    return chosen


def pick_scenarios(args) -> list[dict]:
    only, sides = getattr(args, "scenario", None), getattr(args, "side", None)
    chosen = [s for s in load_scenarios() if not only or s["id"] in only]
    return [{**s, "_sides": sides} for s in chosen] if sides else chosen


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


def estimate(cfg: dict, targets: dict, judges: dict, scenarios: list, rubric: dict, cache: Cache) -> dict:
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

    system = tokens(judging.system_prompt(rubric))
    transcripts = runner.load_transcripts(TRANSCRIPTS)
    jrows = {}
    for name, judge in judges.items():
        jrow = jrows.setdefault(name, {"calls": 0, "in": 0, "out": 0})
        for c in convos:
            brief = tokens(judging.scenario_brief(by_id[c.scenario], c.side))
            convo = tokens(by_id[c.scenario]["accounts"][c.side]["text"]) + tokens(pushback)
            t = transcripts.get(c.id)
            for reply in (1, 2):
                if t and sum(x["role"] == "assistant" for x in t["turns"]) >= reply:
                    if judging.judge_reply(judge, cache, rubric, by_id[c.scenario], t, reply, live=False):
                        continue
                jrow["calls"] += 1
                jrow["in"] += system + brief + convo + reply * est["reply_tokens"]
                jrow["out"] += est["judge_output_tokens"]
    return {"targets": rows, "judges": jrows}


def dollars(row: dict, price: dict, scale: float = 1.0) -> float | None:
    if price.get("input") is None or price.get("output") is None:
        return None
    return (row["in"] * price["input"] + row["out"] * scale * price["output"]) / 1e6


def cmd_plan(args) -> dict:
    cfg, rubric = load_config(), load_rubric()
    targets = pick(cfg["targets"], args.target)
    judges = pick(cfg["judges"], getattr(args, "judge", None))
    scenarios = pick_scenarios(args)
    e = estimate(cfg, targets, judges, scenarios, rubric, Cache(CACHE))
    total = low = high = 0.0
    lines = [f"{'':14} {'model':24} {'convos':>6} {'new calls':>9} {'in tok':>8} {'out tok':>8} {'est. USD':>9}"]

    def add(label, model, row, price, convos=""):
        nonlocal total, low, high
        d = dollars(row, price)
        total += d or 0
        low += dollars(row, price, 0.5) or 0
        high += dollars(row, price, 2.0) or 0
        lines.append(f"{label:14} {model:24} {convos!s:>6} {row['calls']:>9} {row['in']:>8} {row['out']:>8} "
                     f"{('$%.2f' % d) if d is not None else 'no price':>9}")

    for name, row in e["targets"].items():
        add(name, cfg["targets"][name]["model"], row, cfg["targets"][name]["price"], row["conversations"])
    for name, row in e["judges"].items():
        add("judge " + name, cfg["judges"][name]["model"], row, cfg["judges"][name]["price"])
    print("\n".join(lines))
    calls = sum(r["calls"] for r in e["targets"].values()) + sum(r["calls"] for r in e["judges"].values())
    print(f"\n{calls} new calls. Estimated ${total:.2f}, between ${low:.2f} and ${high:.2f} "
          f"if thinking runs half or double the assumed length. Cached calls cost nothing.")
    return {"calls": sum(r["calls"] for r in e["targets"].values()), "usd": total}


def cmd_run(args) -> int:
    cfg, rubric = load_config(), load_rubric()
    targets = pick(cfg["targets"], args.target)
    scenarios = pick_scenarios(args)
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
        print("\nNothing sent. Run again with --yes to make the target calls.")
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
    scenarios = {s["id"]: s for s in load_scenarios()}
    transcripts = runner.load_transcripts(TRANSCRIPTS)
    if args.target:
        transcripts = {k: v for k, v in transcripts.items() if v["target"] in args.target}
    if args.scenario:
        transcripts = {k: v for k, v in transcripts.items() if v["scenario"] in args.scenario}
    if args.sheet:
        with open(os.path.join(RESULTS, "sheet", "key.json"), encoding="utf-8") as f:
            sample = set(json.load(f).values())
        transcripts = {k: v for k, v in transcripts.items() if k in sample}
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

    judges = pick(cfg["judges"], args.judge)
    status = 0
    for name, judge in judges.items():
        pending = [(t, r) for t, r in jobs
                   if not judging.judge_reply(judge, cache, rubric, scenarios[t["scenario"]], t, r, live=False)]
        if pending and not args.yes:
            print(f"judge {name}: {len(pending)} replies need a new call ({len(jobs) - len(pending)} cached). "
                  "Run again with --yes to make them.")
            status = 2
            continue

        def score(job, judge=judge, name=name):
            t, r = job
            try:
                return judging.judge_reply(judge, cache, rubric, scenarios[t["scenario"]], t, r, live=True)
            except providers.ProviderError as e:
                print(f"judge {name}, {t['id']} reply {r}: {e}")
                return None

        with ThreadPoolExecutor(max_workers=cfg.get("concurrency", 4)) as pool:
            fresh = {(r["conversation"], r["reply"]): r for r in pool.map(score, jobs) if r}
        # Keep judgments for transcripts outside this call's filter.
        kept = {(r["conversation"], r["reply"]): r for r in report.load_judgments(judgments_path(name))}
        kept.update(fresh)
        report.write_judgments(list(kept.values()), judgments_path(name))
        health = report.judge_health(list(fresh.values()))
        print(f"judge {name}: {len(fresh)} judgments. Invalid: {health['invalid']}. "
              f"Quotes not found in the reply: {health['quotes_not_found'] or 'none'}.")
    return status


def cmd_report(args) -> int:
    cfg, rubric = load_config(), load_rubric()
    scenarios = {s["id"]: s for s in load_scenarios()}
    by_judge = {name: report.load_judgments(judgments_path(name)) for name in cfg["judges"]}
    by_judge = {k: v for k, v in by_judge.items() if v}
    primary = cfg["primary_judge"]
    if primary not in by_judge:
        print(f"No judgments from the primary judge ({primary}) yet. Run ./eval.sh judge first.")
        return 2
    transcripts = runner.load_transcripts(TRANSCRIPTS)
    body = ["# Results", ""]
    full = len(by_judge[primary])
    for name, judgments in by_judge.items():
        if name != primary and len(judgments) < full:
            body += [f"## Judge {name} ({cfg['judges'][name]['model']}), the cross-check", "",
                     f"Scored {len(judgments)} of the {full} replies, the blind sample. "
                     "Its agreement with the headline judge is below.", ""]
            continue
        targets = sorted({j["target"] for j in judgments})
        nums = report.numbers(judgments, targets, sorted(scenarios))
        health = report.judge_health(judgments)
        role = "the headline" if name == primary else "the cross-check"
        body += [f"## Judge {name} ({cfg['judges'][name]['model']}), {role}", "",
                 report.numbers_markdown(nums, rubric), "",
                 f"{health['judgments']} judgments, {health['invalid']} invalid, "
                 f"quotes not found in the reply: {health['quotes_not_found'] or 'none'}.", ""]
    others = [n for n in by_judge if n != primary]
    if others:
        results = {f"{primary} vs {o}": sheet.agreement(sheet.from_judge(by_judge[primary]),
                                                        sheet.from_judge(by_judge[o])) for o in others}
        body += ["## The judges against each other", "", "Cohen's kappa, n the replies both judges scored.", "",
                 sheet.agreement_markdown(results), ""]
        body += ["Mean lean by model, on the replies both judges scored (higher is closer to the speaker):", "",
                 report.lean_by_judge(by_judge, primary, others), ""]
    worst = report.worst(by_judge[primary], per_target=True)
    os.makedirs(EXAMPLES, exist_ok=True)
    for cid in worst:
        t = transcripts[cid]
        with open(os.path.join(EXAMPLES, f"{cid}.md"), "w", encoding="utf-8") as f:
            f.write(report.worst_markdown(cid, t, scenarios[t["scenario"]], by_judge[primary], rubric))
    body += ["The transcript the headline judge ranks lowest for each model: "
             + ", ".join(f"[{c}](../examples/{c}.md)"
                         + (f" (tied with {report.ties(by_judge[primary], c) - 1} more)"
                            if report.ties(by_judge[primary], c) > 1 else "") for c in worst), ""]
    with open(os.path.join(RESULTS, "report.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(body))
    print("\n".join(body))
    return 0


def cmd_compare(args) -> int:
    """One target against another on one half of the split, by one judge."""
    cfg, rubric = load_config(), load_rubric()
    ids = cfg["split"][args.split]
    judgments = report.load_judgments(judgments_path(args.judge))
    nums = report.numbers(judgments, [args.base, args.treated], ids)
    body = [f"# {args.treated} against {args.base}, {args.split} scenarios ({len(ids)}), judge {args.judge}", "",
            report.numbers_markdown(nums, rubric), ""]
    path = os.path.join(RESULTS, f"compare-{args.treated}-{args.split}-{args.judge}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(body))
    print("\n".join(body))
    return 0


def cmd_sheet(args) -> int:
    cfg, rubric = load_config(), load_rubric()
    scenarios = {s["id"]: s for s in load_scenarios()}
    transcripts = runner.load_transcripts(TRANSCRIPTS)
    out = os.path.join(RESULTS, "sheet")
    try:
        picked = sheet.export(transcripts, scenarios, rubric, cfg["seed"], out)
    except RuntimeError as e:
        print(f"Sheet left as it is: {e}.")
        return 2
    with open(os.path.join(out, "spot-key.json"), encoding="utf-8") as f:
        sheet.build_spot_page(json.load(f), transcripts, scenarios, os.path.join(out, "spot.html"))
    print(f"The judges' sample: {len(picked)} conversations ({2 * len(picked)} replies), in key.json. "
          f"The spot-check: spot.md and spot.csv in {os.path.relpath(out, ROOT)}/. "
          "spot-key.json stays closed until the spot-check is done.")
    return 0


def cmd_agree(args) -> int:
    cfg = load_config()
    out = os.path.join(RESULTS, "sheet")
    with open(os.path.join(out, "key.json"), encoding="utf-8") as f:
        sample_ids = set(json.load(f).values())
    judges = {}
    for name in cfg["judges"]:
        scores = sheet.from_judge(report.load_judgments(judgments_path(name)))
        scores = {k: v for k, v in scores.items() if k[0] in sample_ids}
        if scores:
            judges[name] = scores
    names = list(judges)
    pairs = {f"{a} vs {b}": sheet.agreement(judges[a], judges[b])
             for i, a in enumerate(names) for b in names[i + 1:]}
    md = ["# Agreement", "", f"The judges on the sample of {len(sample_ids)} conversations. "
          "Cohen's kappa, with n the replies both judges scored.", "", sheet.agreement_markdown(pairs), ""]
    spot_csv = os.path.join(out, "spot.csv")
    if sheet.started(spot_csv):
        with open(os.path.join(out, "spot-key.json"), encoding="utf-8") as f:
            spot_key = json.load(f)
        person = sheet.read_sheet(spot_csv, spot_key)
        md += ["## The spot-check", "", "One person's item b and lean on one conversation per model, "
               "beside each judge, as counts.", "", sheet.spot_compare(person, judges), ""]
    with open(os.path.join(RESULTS, "agreement.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(md))
    print("\n".join(md))
    return 0


def cmd_spot(args) -> int:
    out = os.path.join(RESULTS, "sheet")
    with open(os.path.join(out, "spot-key.json"), encoding="utf-8") as f:
        spot_key = json.load(f)
    if args.action == "build":
        transcripts = runner.load_transcripts(TRANSCRIPTS)
        scenarios = {s["id"]: s for s in load_scenarios()}
        n = sheet.build_spot_page(spot_key, transcripts, scenarios, os.path.join(out, "spot.html"))
        print(f"{n} conversations in {os.path.relpath(os.path.join(out, 'spot.html'), ROOT)}")
        return 0
    if not args.file:
        print("spot import needs the saved scores")
        return 2
    n = sheet.write_spot_csv(kit.read_rows(args.file), spot_key, os.path.join(out, "spot.csv"))
    print(f"{n} of {2 * len(spot_key)} replies scored, written to spot.csv")
    return 0


def cmd_kit(args) -> int:
    cfg, rubric = load_config(), load_rubric()
    scenarios = load_scenarios()
    if args.action == "build":
        n = kit.build(scenarios, cfg["kit"]["scenarios"], rubric["pushback"], KIT_PAGE)
        print(f"{n} conversations in {os.path.relpath(KIT_PAGE, ROOT)}")
        return 0
    if not args.file:
        print("kit import needs the file of saved replies")
        return 2
    transcripts = kit.load(args.file, scenarios, cfg["kit"]["target"], rubric["pushback"],
                           cfg["kit"].get("model"))
    os.makedirs(TRANSCRIPTS, exist_ok=True)
    for t in transcripts:
        with open(os.path.join(TRANSCRIPTS, t["id"] + ".json"), "w", encoding="utf-8") as f:
            json.dump(t, f, ensure_ascii=False, indent=1)
    print(f"{len(transcripts)} complete conversations imported as target {cfg['kit']['target']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="conflict_eval")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("check", "plan", "run", "judge", "report", "sheet", "agree"):
        p = sub.add_parser(name)
        if name in ("plan", "run", "judge"):
            p.add_argument("--target", action="append", help="limit to one target (repeatable)")
            p.add_argument("--scenario", action="append", help="limit to one scenario id (repeatable)")
        if name in ("plan", "judge"):
            p.add_argument("--judge", action="append", help="limit to one judge (repeatable)")
        if name == "judge":
            p.add_argument("--sheet", action="store_true", help="only the conversations in the blind sample")
        if name in ("plan", "run"):
            p.add_argument("--side", action="append", choices=["A", "B"], help="limit to one side (repeatable)")
        if name in ("run", "judge"):
            p.add_argument("--dry-run", action="store_true", help="render every prompt, call nothing")
            p.add_argument("--yes", action="store_true", help="make the uncached calls")
    cp = sub.add_parser("compare")
    cp.add_argument("--base", default="claude")
    cp.add_argument("--treated", default="claude-guided")
    cp.add_argument("--split", default="test", help="a split named in config.yaml")
    cp.add_argument("--judge", default="claude")
    sp = sub.add_parser("spot")
    sp.add_argument("action", choices=["build", "import"])
    sp.add_argument("file", nargs="?")
    k = sub.add_parser("kit")
    k.add_argument("action", choices=["build", "import"])
    k.add_argument("file", nargs="?")
    args = ap.parse_args(argv)
    handler = {"check": cmd_check, "plan": lambda a: (cmd_plan(a), 0)[1], "run": cmd_run, "judge": cmd_judge,
               "report": cmd_report, "sheet": cmd_sheet, "agree": cmd_agree, "kit": cmd_kit,
               "spot": cmd_spot, "compare": cmd_compare}[args.cmd]
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
