import unittest

from conflict_eval import canary
from conflict_eval.data import load_scenarios


def judgment(cid, scores, lean):
    items = {k: {"quote": "", "reason": "r", "score": v} for k, v in scores.items()}
    return {"conversation": f"canary.{cid}", "reply": 1, "valid": True,
            "scores": {**items, "lean": {"quote": "", "reason": "r", "value": lean}}}


class Canaries(unittest.TestCase):
    def test_every_canary_names_a_kind_and_a_real_telling(self):
        canaries, kinds = canary.load()
        scenarios = {s["id"]: s for s in load_scenarios()}
        self.assertEqual(len(canaries), 16)
        for c in canaries:
            self.assertIn(c["kind"], kinds)
            self.assertIn(c["side"], scenarios[c["scenario"]]["accounts"])
        self.assertEqual(len(canary.transcripts(canaries, scenarios)), 16)

    def test_score_counts_hits_and_names_misses(self):
        kinds = {"ideal": {"a": "pass", "b": "pass", "c": "pass", "d": "pass", "lean": 0}}
        canaries = [{"id": "x", "kind": "ideal"}, {"id": "y", "kind": "ideal"}]
        judgments = [judgment("x", {"a": "pass", "b": "pass", "c": "pass", "d": "pass"}, 0),
                     judgment("y", {"a": "pass", "b": "fail", "c": "pass", "d": "pass"}, 1)]
        r = canary.score(judgments, canaries, kinds)
        self.assertEqual(r["n"], 2)
        self.assertEqual(r["items"]["b"], (1, 2))
        self.assertEqual(r["lean_exact"], (1, 2))
        self.assertEqual(r["lean_near"], (2, 2))
        self.assertEqual(sorted((m["canary"], m["item"]) for m in r["misses"]), [("y", "b"), ("y", "lean")])


if __name__ == "__main__":
    unittest.main()
