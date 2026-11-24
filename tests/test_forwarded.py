"""The forwarded test: the data, the verdict parse, the reading order, the
counts, the hidden account, and the pipeline end to end on scripted models."""

import json
import os
import re
import tempfile
import threading
import unittest

from conflict_eval import forwarded, providers
from conflict_eval.cache import Cache
from conflict_eval.data import load_config


class Data(unittest.TestCase):
    def test_the_page_has_36_messages_each_with_a_reader(self):
        messages = forwarded.load_messages()
        self.assertEqual([m["n"] for m in messages], list(range(1, 37)))
        self.assertTrue(all(m["message"].strip() and m["reader"].strip() for m in messages))

    def test_the_prompt_drops_its_source_note(self):
        prompt = forwarded.load_prompt()
        self.assertTrue(prompt.startswith("You are {the other person}."))
        self.assertNotIn("method page", prompt)

    def test_the_configured_contexts_resolve(self):
        fcfg = load_config()["forwarded"]
        contexts = forwarded.resolve_contexts(fcfg["compare"], fcfg["contexts"])
        self.assertIsNone(contexts["none"])
        self.assertTrue(contexts["everyones-choice"].startswith("# For an AI that serves a person"))

    def test_contexts_take_none_a_name_or_a_path(self):
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
            f.write("  Be kind.\n")
        try:
            contexts = forwarded.resolve_contexts(["none", f"mine={f.name}"], {})
            self.assertEqual(contexts, {"none": None, "mine": "Be kind."})
        finally:
            os.unlink(f.name)
        with self.assertRaises(ValueError):
            forwarded.resolve_contexts(["none"], {})
        with self.assertRaises(ValueError):
            forwarded.resolve_contexts(["none", "nowhere"], {})
        with self.assertRaises(ValueError):
            forwarded.resolve_contexts(["none", "none"], {})

    def test_the_system_prompt_is_the_line_then_the_context(self):
        self.assertEqual(forwarded.system_prompt("You are a helpful AI assistant.", None),
                         "You are a helpful AI assistant.")
        self.assertEqual(forwarded.system_prompt("You are a helpful AI assistant.", "Rules."),
                         "You are a helpful AI assistant.\n\nRules.")


class Verdict(unittest.TestCase):
    def test_a_clean_last_line_parses(self):
        text = "A lands.\n\nVERDICT A=yes B=no C=mixed RANK=A>C>B\n"
        self.assertEqual(forwarded.parse_verdict(text, "ABC"),
                         {"verdicts": {"A": "yes", "B": "no", "C": "mixed"}, "rank": "A>C>B"})
        self.assertEqual(forwarded.parse_verdict("VERDICT A=mixed B=mixed RANK=A=B  ", "AB")["rank"], "A=B")

    def test_anything_else_gives_no_verdict(self):
        bad = [
            "**VERDICT A=yes B=no RANK=A>B**",           # formatting
            "`VERDICT A=yes B=no RANK=A>B`",
            "VERDICT A=yes B=no RANK=A>B\nThanks!",        # not the last line
            "VERDICT B=no A=yes RANK=A>B",                 # letters out of order
            "VERDICT A=yes B=maybe RANK=A>B",              # not a verdict word
            "VERDICT A=yes B=no RANK=A",                   # rank leaves a letter out
            "VERDICT A=yes B=no RANK=A>B>C",               # rank names a letter not shown
            "VERDICT A=yes B=no C=no RANK=A>B=C",          # three verdicts for two versions
            "verdict a=yes b=no rank=a>b",
            "",
        ]
        for text in bad:
            self.assertIsNone(forwarded.parse_verdict(text, "AB"), text)


class Order(unittest.TestCase):
    def test_the_second_reader_reads_the_reverse_of_the_first(self):
        names = ["none", "short", "full"]
        for n in range(1, 37):
            for s in (1, 2):
                first, second = forwarded.orders(names, 20261124, n, s)
                self.assertEqual(sorted(first), sorted(names))
                self.assertEqual(second, first[::-1])

    def test_the_shuffle_is_seeded(self):
        names = ["none", "short", "full"]
        self.assertEqual(forwarded.orders(names, 1, 5, 1), forwarded.orders(names, 1, 5, 1))
        firsts = {tuple(forwarded.orders(names, 1, n, 1)[0]) for n in range(1, 37)}
        self.assertGreater(len(firsts), 1)


class Render(unittest.TestCase):
    def setUp(self):
        self.template = forwarded.load_prompt()

    def test_three_versions_read_as_the_page_wrote_them(self):
        text = forwarded.render(self.template, "the landlord", "Break my lease.", ["one", "two", "three"])
        self.assertTrue(text.startswith("You are the landlord."))
        self.assertIn("> Break my lease.", text)
        self.assertIn("Below are three versions of the reply, A, B, and C.", text)
        self.assertIn("Then rank A, B, and C from lands best", text)
        self.assertIn("VERDICT A=<yes|mixed|no> B=<yes|mixed|no> C=<yes|mixed|no> RANK=<for example B>A=C>", text)
        self.assertTrue(text.endswith("=== Version C ===\nthree\n=== End of version C ==="))
        self.assertNotIn("{", text)
        self.assertNotIn("and so on", text)

    def test_two_versions_name_two(self):
        text = forwarded.render(self.template, "the mom", "Stop it.", ["one", "two"])
        self.assertIn("Below are two versions of the reply, A and B.", text)
        self.assertIn("Then rank A and B from", text)
        self.assertIn("VERDICT A=<yes|mixed|no> B=<yes|mixed|no> RANK=<for example B>A>", text)
        self.assertNotIn("Version C", text)

    def test_a_changed_page_stops_the_render(self):
        with self.assertRaises(ValueError):
            forwarded.render(self.template.replace("rank A, B, and C", "order them"), "x", "y", ["a", "b"])


class Tally(unittest.TestCase):
    def test_counts_per_context_and_unparsed(self):
        reads = [
            {"message": 1, "verdicts": {"none": "no", "full": "yes"}},
            {"message": 1, "verdicts": {"none": "mixed", "full": "yes"}},
            {"message": 2, "verdicts": None},
        ]
        t = forwarded.tally(reads, ["none", "full"])
        self.assertEqual(t["contexts"]["none"], {"yes": 0, "mixed": 1, "no": 1, "reads": 2})
        self.assertEqual(t["contexts"]["full"], {"yes": 2, "mixed": 0, "no": 0, "reads": 2})
        self.assertEqual(t["unparsed"], 1)


class HiddenAccount(unittest.TestCase):
    def test_the_copy_blanks_the_name_and_email_and_keeps_the_rest(self):
        config = {"oauthAccount": {"emailAddress": "a@b.c", "displayName": "A", "fullName": "A B",
                                   "organizationName": "A's org", "accountUuid": "u-1"},
                  "userID": "x"}
        out = providers.scrubbed_config(config)
        self.assertEqual(out["oauthAccount"], {"emailAddress": "", "displayName": "", "fullName": "",
                                               "organizationName": "", "accountUuid": "u-1"})
        self.assertEqual(out["userID"], "x")
        self.assertEqual(config["oauthAccount"]["emailAddress"], "a@b.c")

    def test_the_scratch_home_leaves_the_real_one_whole(self):
        with tempfile.TemporaryDirectory() as real:
            os.makedirs(os.path.join(real, "Library", "Keychains"))
            with open(os.path.join(real, "Library", "Keychains", "keep"), "w") as f:
                f.write("x")
            with open(os.path.join(real, ".claude.json"), "w") as f:
                json.dump({"oauthAccount": {"emailAddress": "a@b.c"}}, f)
            with providers.scratch_home(real) as home:
                self.assertNotEqual(home, real)
                with open(os.path.join(home, ".claude.json")) as f:
                    self.assertEqual(json.load(f)["oauthAccount"]["emailAddress"], "")
                self.assertTrue(os.path.islink(os.path.join(home, "Library")))
            self.assertFalse(os.path.exists(home))
            self.assertTrue(os.path.exists(os.path.join(real, "Library", "Keychains", "keep")))
            with open(os.path.join(real, ".claude.json")) as f:
                self.assertEqual(json.load(f)["oauthAccount"]["emailAddress"], "a@b.c")

    def test_a_hidden_call_keys_apart_from_a_plain_one(self):
        plain = providers.make({"provider": "claude-cli", "model": "m"})
        hidden = providers.make({"provider": "claude-cli", "model": "m", "hide_account": True})
        self.assertNotIn("home", plain.request([("user", "hi")]))
        self.assertEqual(hidden.request([("user", "hi")])["home"], "scratch")


class ScriptedModel(providers.Provider):
    """A target that writes which context it ran under, and a reader that lands
    the reply written under the context named "good" and misses the rest."""
    name = "scripted"
    env_key = ""

    def __init__(self, cfg):
        super().__init__(cfg)
        self.sends = 0
        self.lock = threading.Lock()

    def request(self, turns, system=None, schema=None):
        return {"model": self.model, "system": system or "", "prompt": turns[-1][1]}

    def send(self, request):
        with self.lock:
            self.sends += 1
        if self.cfg.get("role") == "reader":
            blocks = re.findall(r"=== Version ([A-H]) ===\n(.*?)\n=== End of version \1 ===",
                                request["prompt"], re.S)
            if "garble" in request["prompt"]:
                return {"text": "I liked both."}
            verdicts = " ".join(f"{x}={'yes' if 'GOOD' in body else 'no'}" for x, body in blocks)
            rank = ">".join(x for x, _ in blocks)
            return {"text": f"Read.\n\nVERDICT {verdicts} RANK={rank}"}
        marker = "GOOD" if "be good" in request["system"] else "PLAIN"
        return {"text": f"{marker} reply to: {request['prompt']}"}

    def parse(self, raw):
        return providers.Reply(raw["text"], raw["text"], "end_turn", {"input": 1, "output": 1}, self.model)


class Pipeline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = Cache(os.path.join(self.tmp.name, "cache"))
        self.messages = forwarded.load_messages()[:3]
        self.contexts = {"none": None, "good": "be good"}
        self.target = ScriptedModel({"model": "t-1"})
        self.reader = ScriptedModel({"model": "r-1", "role": "reader"})
        self.cfg = {"seed": 7, "sets": 2, "system": "You are a helpful AI assistant."}

    def tearDown(self):
        self.tmp.cleanup()

    def run_it(self, live=True, messages=None):
        return forwarded.run(messages or self.messages, self.contexts, self.target, self.reader,
                             self.cache, self.cfg, live=live)

    def test_a_plan_counts_without_sending(self):
        r = self.run_it(live=False)
        self.assertEqual(r.plan.replies, 3 * 2 * 2)
        self.assertEqual(r.plan.reads, 3 * 2 * 2)
        self.assertEqual(self.target.sends + self.reader.sends, 0)

    def test_each_version_gets_four_reads_and_the_key_maps_back(self):
        r = self.run_it()
        self.assertEqual(len(r.replies), 12)
        self.assertEqual(len(r.reads), 12)
        t = forwarded.tally(r.reads, list(self.contexts))
        self.assertEqual(t["contexts"]["good"], {"yes": 12, "mixed": 0, "no": 0, "reads": 12})
        self.assertEqual(t["contexts"]["none"], {"yes": 0, "mixed": 0, "no": 12, "reads": 12})
        for m in self.messages:
            per = [x for x in r.reads if x["message"] == m["n"]]
            self.assertEqual(len(per), 4)
            for s in (1, 2):
                one, two = [x for x in per if x["set"] == s]
                self.assertEqual(list(one["key"].values()), list(two["key"].values())[::-1])

    def test_each_reply_runs_under_the_line_then_its_context(self):
        r = self.run_it()
        good = [x for x in r.replies if x["context"] == "good"]
        self.assertTrue(all(x["text"].startswith("GOOD") for x in good))
        self.assertEqual(len({(x["message"], x["set"]) for x in good}), 6)

    def test_a_rerun_calls_nothing_and_changes_nothing(self):
        first = self.run_it()
        sends = self.target.sends, self.reader.sends
        self.assertEqual(sends, (12, 12))
        second = self.run_it()
        self.assertEqual((self.target.sends, self.reader.sends), sends)
        self.assertEqual(first.reads, second.reads)
        self.assertEqual(self.run_it(live=False).plan.replies + self.run_it(live=False).plan.reads, 0)

    def test_a_read_with_no_verdict_line_is_reported_not_guessed(self):
        garbled = [{"n": 99, "message": "garble this", "reader": "the cousin"}]
        r = self.run_it(messages=garbled)
        self.assertEqual(len(r.reads), 4)
        self.assertTrue(all(x["verdicts"] is None for x in r.reads))
        self.assertEqual(forwarded.tally(r.reads, list(self.contexts))["unparsed"], 4)
        self.assertEqual(sum("unparsed" in n for n in r.notes), 4)

    def test_the_report_lists_the_misses(self):
        r = self.run_it()
        out = os.path.join(self.tmp.name, "out")
        md = forwarded.write(r, self.messages, list(self.contexts), out, "t", "r")
        self.assertIn("| good | 12 | 0 | 0 | 12 |", md)
        self.assertIn("| none | 0 | 0 | 12 | 12 |", md)
        self.assertIn("## The reads with a miss", md)
        self.assertTrue(os.path.exists(os.path.join(out, "reads.jsonl")))
        with open(os.path.join(out, "replies.jsonl")) as f:
            self.assertEqual(len(f.readlines()), 12)
