import json
import os
import tempfile
import unittest

from conflict_eval import kit
from conflict_eval.data import load_rubric, load_scenarios


class Kit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.scenarios = load_scenarios()
        self.pushback = load_rubric()["pushback"]
        self.ids = [s["id"] for s in self.scenarios[:2]]

    def tearDown(self):
        self.tmp.cleanup()

    def test_build_embeds_every_telling_exactly(self):
        out = os.path.join(self.tmp.name, "kit.html")
        n = kit.build(self.scenarios, self.ids, self.pushback, out)
        self.assertEqual(n, 4)
        with open(out, encoding="utf-8") as f:
            page = f.read()
        self.assertNotIn("/*__KIT_DATA__*/", page)
        data = json.loads(page.split("const KIT = ", 1)[1].split(";\n", 1)[0].replace("<\\/", "</"))
        first = self.scenarios[0]["accounts"]["A"]["text"].strip()
        self.assertEqual(data["conversations"][0]["telling"], first)
        self.assertEqual(data["pushback"], self.pushback)

    def test_load_keeps_complete_pairs_from_a_folder(self):
        folder = os.path.join(self.tmp.name, "replies")
        os.makedirs(folder)
        sid = self.ids[0]
        docs = {f"{sid}.A": {"reply1": "first", "reply2": "second", "app": "Gemini app", "model_shown": "Pro"},
                f"{sid}.B": {"reply1": "only one", "reply2": ""}}
        for cid, body in docs.items():
            with open(os.path.join(folder, cid + ".json"), "w") as f:
                json.dump({"id": cid, "version": 3, "data": body}, f)
        out = kit.load(folder, self.scenarios, "gemini-app", self.pushback)
        self.assertEqual([t["id"] for t in out], [f"{sid}.A.gemini-app"])
        turns = out[0]["turns"]
        self.assertEqual([t["role"] for t in turns], ["user", "assistant", "user", "assistant"])
        self.assertEqual(turns[2]["text"], self.pushback)
        self.assertEqual(turns[1]["model"], "Gemini app Pro")

    def test_load_reads_a_list_export(self):
        path = os.path.join(self.tmp.name, "rows.json")
        sid = self.ids[1]
        with open(path, "w") as f:
            json.dump([{"id": f"{sid}.B", "data": {"reply1": "a", "reply2": "b"}}], f)
        out = kit.load(path, self.scenarios, "gemini-app", self.pushback)
        self.assertEqual(out[0]["side"], "B")


if __name__ == "__main__":
    unittest.main()
