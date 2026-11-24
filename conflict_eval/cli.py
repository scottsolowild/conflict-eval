"""The command line. ./eval.sh -h lists the commands."""

from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

from . import judge as judging
from . import asker, canary, forwarded, kit, providers, report, runner, sheet
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


def cmd_canary(args) -> int:
    """Score the canaries with each judge, and report how often each judge
    lands on the score a canary was written to earn."""
    cfg, rubric = load_config(), load_rubric()
    scenarios = {s["id"]: s for s in load_scenarios()}
    canaries, kinds = canary.load()
    transcripts = canary.transcripts(canaries, scenarios)
    cache = Cache(CACHE)
    results = {}
    for name, judge in pick(cfg["judges"], args.judge).items():
        path = os.path.join(JUDGMENTS, f"canary-{name}.jsonl")
        pending = [t for t in transcripts
                   if not judging.judge_reply(judge, cache, rubric, scenarios[t["scenario"]], t, 1, live=False)]
        if pending and not args.yes:
            print(f"judge {name}: {len(pending)} canaries need a new call. Run again with --yes to make them.")
        def one(t, judge=judge, name=name):
            try:
                return judging.judge_reply(judge, cache, rubric, scenarios[t["scenario"]], t, 1, live=args.yes)
            except providers.ProviderError as e:
                print(f"judge {name}, {t['id']}: {str(e)[:160]}")
                return None
        with ThreadPoolExecutor(max_workers=cfg.get("concurrency", 4)) as pool:
            records = [r for r in pool.map(one, transcripts) if r]
        if records:
            report.write_judgments(records, path)
            results[name] = canary.score(records, canaries, kinds)
    if not results:
        print("No canary scores yet.")
        return 2
    md = canary.markdown(results, rubric)
    with open(os.path.join(RESULTS, "canaries.md"), "w", encoding="utf-8") as f:
        f.write(md)
    print(md)
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


def cmd_forwarded(args) -> int:
    """plan, run, or report the forwarded test on two or more contexts."""
    cfg = load_config()
    fcfg = cfg["forwarded"]
    try:
        contexts = forwarded.resolve_contexts(args.context or fcfg["compare"], fcfg.get("contexts") or {})
    except (ValueError, OSError) as e:
        print(e)
        return 2
    names = list(contexts)
    messages = forwarded.load_messages()
    if args.message:
        known = {m["n"] for m in messages}
        unknown = sorted(set(args.message) - known)
        if unknown:
            print(f"no message numbered {unknown}; the page has 1 to {max(known)}")
            return 2
        messages = [m for m in messages if m["n"] in args.message]
    target_name, reader_name = args.target or fcfg["target"], args.reader or fcfg["reader"]
    hide = {"hide_account": bool(fcfg.get("hide_account"))}
    if target_name not in cfg["targets"] or reader_name not in cfg["judges"]:
        print(f"name a target under targets: and a reader under judges: in config.yaml")
        return 2
    target = providers.make({**cfg["targets"][target_name], **hide})
    reader = providers.make({**cfg["judges"][reader_name], **hide})
    for role, p in (("target", target), ("reader", reader)):
        why = p.missing()
        if why:
            print(f"{role} {p.namespace}: {why}")
            return 2
    out = os.path.join(RESULTS, "forwarded", f"{target_name}.{'+'.join(names)}")
    cache = Cache(CACHE)
    target_label = f"{target_name} ({cfg['targets'][target_name]['model']})"
    reader_label = f"{reader_name} ({cfg['judges'][reader_name]['model']})"

    if getattr(args, "dry_run", False):
        dry = os.path.join(RESULTS, "dry-run", "forwarded")
        os.makedirs(dry, exist_ok=True)
        template = forwarded.load_prompt()
        for m in messages:
            for name in names:
                request = target.request([("user", m["message"])],
                                         system=forwarded.system_prompt(fcfg["system"], contexts[name]))
                with open(os.path.join(dry, f"{m['n']:02}.reply.{name}.json"), "w", encoding="utf-8") as f:
                    json.dump(request, f, ensure_ascii=False, indent=1)
            order = forwarded.orders(names, fcfg["seed"], m["n"], 1)[0]
            stand_ins = [f"<the reply under {name}>" for name in order]
            with open(os.path.join(dry, f"{m['n']:02}.read.md"), "w", encoding="utf-8") as f:
                f.write(forwarded.render(template, m["reader"], m["message"], stand_ins))
        print(f"Wrote {len(messages) * len(names)} reply requests and {len(messages)} reader prompts to "
              f"{os.path.relpath(dry, ROOT)}/. Nothing was sent.")
        return 0

    est = cfg["estimate"]
    result = forwarded.run(messages, contexts, target, reader, cache, fcfg, live=False,
                           concurrency=8, reply_tokens=est["reply_tokens"])
    if args.action in ("plan", "run"):
        plan = result.plan
        reply_usd = forwarded_usd(plan.reply_in, plan.replies * est["target_output_tokens"],
                                  cfg["targets"][target_name]["price"])
        read_usd = forwarded_usd(plan.read_in, plan.reads * est["judge_output_tokens"],
                                 cfg["judges"][reader_name]["price"])
        usd = (reply_usd or 0) + (read_usd or 0)
        priced = reply_usd is not None and read_usd is not None
        print(f"{len(messages)} messages, {len(names)} contexts ({', '.join(names)}), "
              f"{fcfg.get('sets', 2)} sets, {forwarded.READERS} readers a set.")
        print(f"New calls: {plan.replies} replies by {target_label}, {plan.reads} reads by {reader_label}. "
              + (f"Estimated ${usd:.2f}." if priced else "No price in config.yaml for one of them.")
              + " Cached calls cost nothing.")
        if args.action == "plan":
            return 0
        if (plan.replies or plan.reads) and not args.yes:
            print("Nothing sent. Run again with --yes to make the calls.")
            return 2
        result = forwarded.run(messages, contexts, target, reader, cache, fcfg, live=True,
                               concurrency=cfg.get("concurrency", 4), reply_tokens=est["reply_tokens"])
    if not result.reads:
        print("No reads yet. Run ./eval.sh forwarded run --yes first.")
        return 2
    md = forwarded.write(result, messages, names, out, target_label, reader_label)
    print(md)
    print(f"Written to {os.path.relpath(out, ROOT)}/")
    return 0


def cmd_asker(args) -> int:
    """plan, run, or report the 48-message test on two sets of instructions."""
    cfg = load_config()
    fcfg = cfg["forwarded"]
    try:
        contexts = forwarded.resolve_contexts(args.context or [], fcfg.get("contexts") or {})
    except (ValueError, OSError) as e:
        print(e)
        return 2
    names = list(contexts)
    if len(names) != 2 or any(contexts[n] is None for n in names) or "same" in names:
        print("the 48-message test compares two sets of instructions with text, named apart from 'same': "
              "--context NAME --context NAME=PATH")
        return 2
    messages = asker.load_messages()
    if args.batch:
        groups = asker.batches(messages)
        if any(b < 1 or b > len(groups) for b in args.batch):
            print(f"the batches run 1 to {len(groups)}")
            return 2
        messages = [m for b in sorted(set(args.batch)) for m in groups[b - 1]]
    reader_name = args.reader or fcfg["reader"]
    if reader_name not in cfg["judges"]:
        print("name a reader under judges: in config.yaml")
        return 2
    reader = providers.make({**cfg["judges"][reader_name], "hide_account": bool(fcfg.get("hide_account"))})
    why = reader.missing()
    if why:
        print(f"reader {reader.namespace}: {why}")
        return 2
    reader_label = f"{reader_name} ({cfg['judges'][reader_name]['model']})"
    out = os.path.join(RESULTS, "asker", f"{reader_name}.{'+'.join(names)}")
    cache = Cache(CACHE)

    if args.dry_run:
        dry = os.path.join(RESULTS, "dry-run", "asker")
        os.makedirs(dry, exist_ok=True)
        template = asker.load_prompt()
        n = 0
        for b, batch in enumerate(asker.batches(messages), 1):
            for k in range(1, asker.READERS + 1):
                sides = asker.key(names, k)
                with open(os.path.join(dry, f"batch-{b}.reader-{k}.md"), "w", encoding="utf-8") as f:
                    f.write(asker.render(template, contexts[sides["X"]], contexts[sides["Y"]], batch))
                n += 1
        print(f"Wrote {n} reader prompts to {os.path.relpath(dry, ROOT)}/. Nothing was sent.")
        return 0

    result = asker.run(messages, contexts, reader, cache, live=False, concurrency=8)
    if args.action in ("plan", "run"):
        usd = forwarded_usd(result.pending_in, result.pending * cfg["estimate"]["judge_output_tokens"],
                            cfg["judges"][reader_name]["price"])
        print(f"{len(messages)} messages in {len(asker.batches(messages))} batches, {asker.READERS} readers a batch. "
              f"New calls: {result.pending} reads by {reader_label}. "
              + (f"Estimated ${usd:.2f}." if usd is not None else "No price in config.yaml.")
              + " Cached calls cost nothing.")
        if args.action == "plan":
            return 0
        if result.pending and not args.yes:
            print("Nothing sent. Run again with --yes to make the calls.")
            return 2
        result = asker.run(messages, contexts, reader, cache, live=True, concurrency=cfg.get("concurrency", 4))
    if not result.reads:
        print("No reads yet. Run ./eval.sh asker run --yes first.")
        return 2
    md = asker.write(result, messages, names, out, reader_label)
    print(md)
    print(f"Written to {os.path.relpath(out, ROOT)}/")
    return 0


def forwarded_usd(tokens_in: int, tokens_out: int, price: dict) -> float | None:
    if price.get("input") is None or price.get("output") is None:
        return None
    return (tokens_in * price["input"] + tokens_out * price["output"]) / 1e6


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
    cn = sub.add_parser("canary")
    cn.add_argument("--judge", action="append", help="limit to one judge (repeatable)")
    cn.add_argument("--yes", action="store_true", help="make the uncached judge calls")
    sp = sub.add_parser("spot")
    sp.add_argument("action", choices=["build", "import"])
    sp.add_argument("file", nargs="?")
    fw = sub.add_parser("forwarded")
    fw.add_argument("action", choices=["plan", "run", "report"])
    fw.add_argument("--context", action="append",
                    help="'none', a name under forwarded.contexts, or NAME=PATH (repeatable, two or more)")
    fw.add_argument("--target", help="the target that writes the replies, a name under targets:")
    fw.add_argument("--reader", help="the model that reads them as the other person, a name under judges:")
    fw.add_argument("--message", action="append", type=int, help="limit to one message by number (repeatable)")
    fw.add_argument("--dry-run", action="store_true", help="render every reply request and reader prompt, call nothing")
    fw.add_argument("--yes", action="store_true", help="make the uncached calls")
    ak = sub.add_parser("asker")
    ak.add_argument("action", choices=["plan", "run", "report"])
    ak.add_argument("--context", action="append",
                    help="a name under forwarded.contexts or NAME=PATH, twice: the first is X for readers 1 and 3")
    ak.add_argument("--reader", help="the model that reads, a name under judges:")
    ak.add_argument("--batch", action="append", type=int, help="limit to one batch of eight, 1 to 6 (repeatable)")
    ak.add_argument("--dry-run", action="store_true", help="render every reader prompt, call nothing")
    ak.add_argument("--yes", action="store_true", help="make the uncached calls")
    k = sub.add_parser("kit")
    k.add_argument("action", choices=["build", "import"])
    k.add_argument("file", nargs="?")
    args = ap.parse_args(argv)
    handler = {"check": cmd_check, "plan": lambda a: (cmd_plan(a), 0)[1], "run": cmd_run, "judge": cmd_judge,
               "report": cmd_report, "sheet": cmd_sheet, "agree": cmd_agree, "kit": cmd_kit,
               "spot": cmd_spot, "compare": cmd_compare,
               "canary": cmd_canary, "forwarded": cmd_forwarded, "asker": cmd_asker}[args.cmd]
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
