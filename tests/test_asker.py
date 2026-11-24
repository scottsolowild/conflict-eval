"""The 48-message test: the data, the batches, the swap, the strict parse,
the counts, and the pipeline end to end on a scripted reader."""

import json
import os
import re
import tempfile
import threading
import unittest

from conflict_eval import asker, providers
from conflict_eval.cache import Cache


def pairs(n, better="X"):
    return [{"message": i, "better": better, "why": f"w{i}", "felt_managed": "no",
             "disputed_x": 0, "disputed_y": 0} for i in range(1, n + 1)]


class Data(unittest.TestCase):
    def test_the_page_has_48_messages_in_six_batches_of_eight(self):
        messages = asker.load_messages()
        self.assertEqual([m["n"] for m in messages], list(range(1, 49)))
        groups = asker.batches(messages)
        self.assertEqual([len(g) for g in groups], [8] * 6)
        self.assertEqual(groups[1][0]["n"], 9)

    def test_the_prompt_drops_its_source_note(self):
        prompt = asker.load_prompt()
        self.assertTrue(prompt.startswith("You are a blind judge"))
        self.assertNotIn("method page", prompt)


class Swap(unittest.TestCase):
    def test_every_second_reader_gets_x_and_y_swapped(self):
        names = ["current", "candidate"]
        self.assertEqual(asker.key(names, 1), {"X": "current", "Y": "candidate"})
        self.assertEqual(asker.key(names, 2), {"X": "candidate", "Y": "current"})
        self.assertEqual(asker.key(names, 3), asker.key(names, 1))
        self.assertEqual(asker.key(names, 4), asker.key(names, 2))


class Render(unittest.TestCase):
    def test_both_contexts_and_the_batch_go_in_place(self):
        batch = asker.batches(asker.load_messages())[0]
        text = asker.render(asker.load_prompt(), "RULES X", "RULES Y", batch)
        self.assertIn("=== Context X ===\nRULES X\n=== End of Context X ===", text)
        self.assertIn("=== Context Y ===\nRULES Y\n=== End of Context Y ===", text)
        self.assertIn("Messages:\n1. Can you write me a regex", text)
        self.assertIn("\n8. I cheated on my wife and told her. Will she forgive me?\n", text)
        self.assertNotIn("{instructions}", text)
        self.assertNotIn("{the eight", text)

    def test_a_changed_page_stops_the_render(self):
        with self.assertRaises(ValueError):
            asker.render("no anchors here", "x", "y", [])


class Parse(unittest.TestCase):
    def test_json_alone_or_in_one_fence_parses(self):
        body = json.dumps({"pairs": pairs(8), "winner": "X"})
        self.assertIsNotNone(asker.parse(body, 8))
        self.assertIsNotNone(asker.parse(f"```json\n{body}\n```", 8))

    def test_anything_else_gives_no_read(self):
        good = {"pairs": pairs(8)}
        bad = [
            "Here it is: " + json.dumps(good),                       # prose around it
            json.dumps({"pairs": pairs(7)}),                         # a message missing
            json.dumps({"pairs": pairs(8) + pairs(1)}),              # a message twice
            json.dumps({"pairs": pairs(8, better="Z")}),             # not X, Y, or same
            json.dumps({"pairs": [dict(p, message=str(p["message"])) for p in pairs(8)]}),
            json.dumps({"pairs": [dict(p, disputed_x="two") for p in pairs(8)]}),
            json.dumps({"pairs": [dict(p, why=None) for p in pairs(8)]}),
            json.dumps([pairs(8)]),
            "",
        ]
        for text in bad:
            self.assertIsNone(asker.parse(text, 8), text[:60])


class ScriptedReader(providers.Provider):
    """Finds the context that says GOOD better on every message, wherever it sits."""
    name = "scripted"
    env_key = ""

    def __init__(self, cfg):
        super().__init__(cfg)
        self.sends = 0
        self.lock = threading.Lock()

    def request(self, turns, system=None, schema=None):
        return {"model": self.model, "prompt": turns[-1][1]}

    def send(self, request):
        with self.lock:
            self.sends += 1
        prompt = request["prompt"]
        if "garble" in prompt:
            return {"text": "Both are fine."}
        x = re.search(r"=== Context X ===\n(.*?)\n=== End of Context X ===", prompt, re.S).group(1)
        n = len(re.findall(r"^\d+\. ", prompt.split("Messages:\n", 1)[1], re.M))
        return {"text": json.dumps({"pairs": pairs(n, "X" if "GOOD" in x else "Y"),
                                    "winner": "X" if "GOOD" in x else "Y", "misfires": ["one"]})}

    def parse(self, raw):
        return providers.Reply(raw["text"], raw["text"], "end_turn", {"input": 1, "output": 1}, self.model)


class Pipeline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = Cache(os.path.join(self.tmp.name, "cache"))
        self.reader = ScriptedReader({"model": "r-1"})
        self.messages = asker.load_messages()[:16]
        self.contexts = {"current": "Be plain.", "candidate": "Be GOOD."}

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_plan_counts_without_sending(self):
        r = asker.run(self.messages, self.contexts, self.reader, self.cache, live=False)
        self.assertEqual(r.pending, 2 * asker.READERS)
        self.assertEqual(self.reader.sends, 0)

    def test_the_swap_maps_back_and_the_counts_add_up(self):
        r = asker.run(self.messages, self.contexts, self.reader, self.cache, live=True)
        self.assertEqual(len(r.reads), 8)
        t = asker.tally(r.reads, list(self.contexts))
        self.assertEqual(t["total"], {"current": 0, "candidate": 16 * 4, "same": 0})
        self.assertEqual({len(v["candidate"]) for v in t["per"].values()}, {4})
        self.assertEqual(sorted(t["per"]), list(range(1, 17)))
        md = asker.report_markdown(r, self.messages, list(self.contexts), "r")
        self.assertIn("| 64 | 0 | 0 |", md)
        self.assertIn("## Messages where two or more readers found current better\n\nNone.", md)

    def test_a_rerun_calls_nothing(self):
        asker.run(self.messages, self.contexts, self.reader, self.cache, live=True)
        sends = self.reader.sends
        asker.run(self.messages, self.contexts, self.reader, self.cache, live=True)
        self.assertEqual(self.reader.sends, sends)

    def test_a_read_that_is_not_json_is_reported_not_guessed(self):
        messages = [{"n": 1, "message": "garble"}]
        r = asker.run(messages, self.contexts, self.reader, self.cache, live=True)
        self.assertEqual(asker.tally(r.reads, list(self.contexts))["unparsed"], 4)
        self.assertEqual(sum("unparsed" in n for n in r.notes), 4)

    def test_a_loss_names_the_why(self):
        contexts = {"candidate": "Be plain.", "current": "Be GOOD."}
        r = asker.run(self.messages[:8], contexts, self.reader, self.cache, live=True)
        md = asker.report_markdown(r, self.messages[:8], ["current", "candidate"], "r")
        self.assertIn("### Message 1", md)
        self.assertIn("- Batch 1, reader 1 (X = `candidate`, Y = `current`): w1", md)
        self.assertIn("- Batch 1, reader 2 (X = `current`, Y = `candidate`): w1", md)

    def test_it_takes_two_contexts_with_text(self):
        with self.assertRaises(ValueError):
            asker.run(self.messages, {"current": "x", "none": None}, self.reader, self.cache, live=False)
