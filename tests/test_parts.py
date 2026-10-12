"""The pieces that touch provider formats and the rubric, checked offline."""

import unittest

from conflict_eval import judge as judging
from conflict_eval import providers
from conflict_eval.data import check_scenario, load_rubric, load_scenarios


class Scenarios(unittest.TestCase):
    def test_every_scenario_meets_its_rules(self):
        for s in load_scenarios():
            self.assertEqual(check_scenario(s), [], s["id"])

    def test_a_lopsided_pair_is_caught(self):
        s = load_scenarios()[0]
        s["accounts"]["A"]["text"] += " extra" * 100
        self.assertTrue(any("length" in p for p in check_scenario(s)))


class Rubric(unittest.TestCase):
    def test_render_carries_every_item_and_the_pushback(self):
        rubric = load_rubric()
        text = judging.render_rubric(rubric)
        for k in judging.ITEMS:
            self.assertIn(rubric["items"][k]["name"], text)
        self.assertIn(rubric["pushback"], text)
        self.assertIn("+2:", text)

    def test_schema_is_closed_at_every_level(self):
        def walk(node):
            if node.get("type") == "object":
                self.assertIs(node["additionalProperties"], False)
                self.assertEqual(sorted(node["required"]), sorted(node["properties"]))
                for child in node["properties"].values():
                    walk(child)
        walk(judging.SCHEMA)


class Quotes(unittest.TestCase):
    def test_found_through_markdown_and_curly_quotes(self):
        reply = "**You're** carrying a lot.  It “makes sense” to feel shut out."
        self.assertTrue(judging.quote_found("you're carrying a lot", reply))
        self.assertTrue(judging.quote_found('It "makes sense" to feel shut out.', reply))

    def test_invented_quote_is_not_found(self):
        self.assertFalse(judging.quote_found("he went behind your back", "You felt shut out."))

    def test_empty_quote_counts_as_found(self):
        self.assertTrue(judging.quote_found("", "anything"))


class AnthropicFormat(unittest.TestCase):
    p = providers.Anthropic({"model": "claude-opus-5-5", "effort": "medium", "max_tokens": 16000})

    def test_target_request_has_no_system_prompt(self):
        req = self.p.request([("user", "hi")])
        self.assertNotIn("system", req)
        self.assertEqual(req["output_config"], {"effort": "medium"})

    def test_judge_request_carries_system_and_schema(self):
        req = self.p.request([("user", "hi")], system="rubric", schema=judging.SCHEMA)
        self.assertEqual(req["system"], "rubric")
        self.assertEqual(req["output_config"]["format"]["type"], "json_schema")

    def test_parse_keeps_thinking_for_the_replay_and_text_for_the_reader(self):
        raw = {"model": "claude-opus-5-5", "stop_reason": "end_turn",
               "usage": {"input_tokens": 10, "output_tokens": 20},
               "content": [{"type": "thinking", "thinking": "", "signature": "sig"},
                           {"type": "text", "text": "Hello.", "citations": None}]}
        r = self.p.parse(raw)
        self.assertEqual(r.text, "Hello.")
        self.assertEqual(r.turn, [{"type": "thinking", "thinking": "", "signature": "sig"},
                                  {"type": "text", "text": "Hello."}])
        self.assertEqual(r.usage, {"input": 10, "output": 20})

    def test_refusal_is_flagged(self):
        r = self.p.parse({"stop_reason": "refusal", "content": [], "usage": {}})
        self.assertTrue(r.refused)


class GeminiFormat(unittest.TestCase):
    p = providers.Gemini({"model": "gemini-3.1-pro-preview", "max_tokens": 16000})

    def test_replay_keeps_the_models_own_content(self):
        turn = {"role": "model", "parts": [{"text": "Hi.", "thoughtSignature": "abc"}]}
        req = self.p.request([("user", "a"), ("assistant", turn), ("user", "b")])
        self.assertEqual(req["contents"][1], turn)
        self.assertNotIn("systemInstruction", req)

    def test_parse_drops_thought_parts_and_counts_thinking_tokens(self):
        raw = {"modelVersion": "gemini-3.1-pro-preview",
               "candidates": [{"finishReason": "STOP", "content": {"role": "model", "parts": [
                   {"text": "thinking...", "thought": True}, {"text": "Answer."}]}}],
               "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 7, "thoughtsTokenCount": 30}}
        r = self.p.parse(raw)
        self.assertEqual(r.text, "Answer.")
        self.assertEqual(r.usage, {"input": 5, "output": 37})

    def test_a_blocked_prompt_is_flagged(self):
        r = self.p.parse({"promptFeedback": {"blockReason": "SAFETY"}})
        self.assertTrue(r.refused)
        self.assertEqual(r.stop, "SAFETY")


class Keys(unittest.TestCase):
    def test_missing_key_names_the_variable_and_not_a_value(self):
        p = providers.OpenAI({"model": "some-model"})
        import os
        saved = os.environ.pop("OPENAI_API_KEY", None)
        try:
            self.assertEqual(p.missing(), "OPENAI_API_KEY is not set")
        finally:
            if saved is not None:
                os.environ["OPENAI_API_KEY"] = saved

    def test_no_model_id_is_a_reason_to_skip(self):
        self.assertEqual(providers.OpenAI({"model": None}).missing(), "no model id in config.yaml")


if __name__ == "__main__":
    unittest.main()


class QuoteEllipsis(unittest.TestCase):
    def test_passages_joined_by_an_ellipsis_are_each_checked(self):
        reply = "Where you're right: the date. Where I don't think you're right: the tone."
        self.assertTrue(judging.quote_found("Where you're right: ... Where I don't think you're right:", reply))
        self.assertFalse(judging.quote_found("Where you're right: ... you lied", reply))


class LeanByJudge(unittest.TestCase):
    def test_means_over_the_replies_both_judges_scored(self):
        from conflict_eval import report

        def j(cid, reply, lean):
            item = {"quote": "", "reason": "", "score": "pass"}
            return {"conversation": cid, "reply": reply, "valid": True,
                    "scores": {**{k: item for k in "abcd"}, "lean": {"quote": "", "reason": "", "value": lean}}}

        by_judge = {"claude": [j("s.A.claude", 1, 2), j("s.A.claude", 2, 0), j("s.A.gemini-app", 1, 1)],
                    "gemini": [j("s.A.claude", 1, 0), j("s.A.gemini-app", 1, 2)]}
        table = report.lean_by_judge(by_judge, "claude", ["gemini"])
        self.assertIn("| claude | 1 | +2.00 | +0.00 |", table)
        self.assertIn("| gemini-app | 1 | +1.00 | +2.00 |", table)


class WorstIsStable(unittest.TestCase):
    def test_ties_break_the_same_way_every_run(self):
        from conflict_eval import report
        item = {"quote": "", "reason": "", "score": "fail"}
        def j(cid, reply):
            return {"conversation": cid, "reply": reply, "valid": True,
                    "scores": {**{k: item for k in "abcd"}, "lean": {"quote": "", "reason": "", "value": 2}}}
        records = [j(f"s{i:02d}.A.m", r) for i in range(8) for r in (1, 2)]
        self.assertEqual(report.worst(records), ["s00.A.m", "s01.A.m", "s02.A.m"])
        self.assertEqual(report.worst(list(reversed(records))), ["s00.A.m", "s01.A.m", "s02.A.m"])


class NumbersFilter(unittest.TestCase):
    def test_item_rates_count_only_the_named_scenarios(self):
        from conflict_eval import report
        def j(cid, reply, score):
            item = {"quote": "", "reason": "", "score": score}
            return {"conversation": cid, "reply": reply, "valid": True,
                    "scores": {**{k: item for k in "abcd"}, "lean": {"quote": "", "reason": "", "value": 0}}}
        records = [j("s1.A.m", 1, "pass"), j("s2.A.m", 1, "fail")]
        n = report.numbers(records, ["m"], ["s1"])["m"]
        self.assertEqual(n["items"][("a", 1)], (1, 1, 1.0))
        self.assertEqual(n["conversations"], 1)
