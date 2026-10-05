import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agreement  # noqa: E402
import build_pool  # noqa: E402
import make_labeling_sheet  # noqa: E402
import run_judge  # noqa: E402
import run_pilot  # noqa: E402


class TestAllocate(unittest.TestCase):
    def test_sums_within_capacity(self):
        strata = {"a": list(range(10)), "b": list(range(20)), "c": list(range(5))}
        alloc = make_labeling_sheet.allocate(strata, 20, min_per=2)
        self.assertEqual(sum(alloc.values()), 20)
        for key, value in alloc.items():
            self.assertLessEqual(value, len(strata[key]))

    def test_caps_at_pool_size(self):
        strata = {"a": [1, 2], "b": [1, 2, 3]}
        alloc = make_labeling_sheet.allocate(strata, 100, min_per=1)
        self.assertEqual(sum(alloc.values()), 5)


class TestSplit(unittest.TestCase):
    def test_deterministic_and_uses_answer(self):
        pair = {"source": "hf:x:intent=a", "cached_response": "Foo bar"}
        self.assertEqual(build_pool.split_for(pair), build_pool.split_for(dict(pair)))
        self.assertIn(build_pool.split_for(pair), {"dev", "test"})


class TestJudgeParsing(unittest.TestCase):
    def test_extract_json(self):
        self.assertEqual(run_judge.extract_json('```json\n{"label":"hit"}\n```'), {"label": "hit"})
        self.assertIsNone(run_judge.extract_json("no json here"))

    def test_to_label(self):
        for value in ("hit", "YES", True, 1):
            self.assertIs(run_judge.to_label(value), True)
        for value in ("miss", "no", False, 0):
            self.assertIs(run_judge.to_label(value), False)
        self.assertIsNone(run_judge.to_label("maybe"))


class TestKappa(unittest.TestCase):
    def test_perfect(self):
        self.assertAlmostEqual(agreement.kappa([(True, True), (False, False)]), 1.0)

    def test_chance(self):
        pairs = [(True, True), (True, False), (False, True), (False, False)]
        self.assertAlmostEqual(agreement.kappa(pairs), 0.0)

    def test_confusion(self):
        table = agreement.confusion([(True, False), (False, False)])
        self.assertEqual(table, {"hit_hit": 0, "hit_miss": 1, "miss_hit": 0, "miss_miss": 1})


class TestGoldLabels(unittest.TestCase):
    def test_parse_gold(self):
        self.assertIs(run_pilot.parse_gold_label("hit"), True)
        self.assertIs(run_pilot.parse_gold_label("miss"), False)
        self.assertIsNone(run_pilot.parse_gold_label(""))

    def test_sheet_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            key = {
                "rows": {
                    "r_00000": {"sample_id": "s_0000", "is_duplicate": False, "split": "dev"},
                    "r_00001": {"sample_id": "s_0001", "is_duplicate": False, "split": "dev"},
                },
                "samples": {
                    "s_0000": {"id": "pool_00000"},
                    "s_0001": {"id": "pool_00001"},
                },
            }
            key_path = Path(tmp) / "key.json"
            key_path.write_text(json.dumps(key))
            sheet = Path(tmp) / "sheet.csv"
            sheet.write_text(
                "row_id,domain,category,cached_query,cached_response,new_query,label,confidence,notes\n"
                'r_00000,d,c,q,r,n,hit,,\n'
                'r_00001,d,c,q,r,n,miss,,\n'
            )
            out = Path(tmp) / "labels.jsonl"
            import sheet_to_labels
            argv = sys.argv
            sys.argv = ["sheet_to_labels.py", "--sheet", str(sheet), "--key", str(key_path), "--out", str(out)]
            try:
                sheet_to_labels.main()
            finally:
                sys.argv = argv
            labels = {json.loads(line)["id"]: json.loads(line)["should_hit"] for line in out.read_text().splitlines()}
            self.assertEqual(labels, {"pool_00000": True, "pool_00001": False})


class TestSheetCLI(unittest.TestCase):
    def test_builds_sheet_with_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            pool = Path(tmp) / "pool.jsonl"
            rows = []
            for i in range(30):
                rows.append({
                    "id": f"pool_{i:05d}", "category": ["paraphrase", "unrelated", "entity_swap"][i % 3],
                    "domain": "customer_support", "source": "hf:x", "split": "dev",
                    "cached_query": f"q{i}", "cached_response": f"r{i}", "new_query": f"n{i}",
                    "heuristic_label": True, "heuristic_source": "heuristic", "notes": "",
                })
            pool.write_text("\n".join(json.dumps(r) for r in rows))
            result = subprocess.run(
                [sys.executable, str(ROOT / "make_labeling_sheet.py"), "--pool", str(pool),
                 "--n", "12", "--duplicates", "4", "--seed", "1",
                 "--sheet", str(Path(tmp) / "s.csv"), "--key", str(Path(tmp) / "k.json"),
                 "--sample", str(Path(tmp) / "sample.jsonl")],
                capture_output=True, text=True, cwd=str(ROOT),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            key = json.loads((Path(tmp) / "k.json").read_text())
            self.assertEqual(key["n_unique"], 12)
            self.assertEqual(key["n_duplicates"], 4)
            self.assertEqual(key["n_rows"], 16)


if __name__ == "__main__":
    unittest.main()
