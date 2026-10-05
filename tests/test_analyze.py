import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import analyze  # noqa: E402
import run_llm_baseline  # noqa: E402


def rec(score, label, domain="d"):
    return {"score": score, "label": label, "domain": domain}


class TestAnalyzeMetrics(unittest.TestCase):
    def test_auc(self):
        self.assertAlmostEqual(analyze.auc([rec(0.9, True), rec(0.8, True), rec(0.2, False), rec(0.1, False)]), 1.0)

    def test_precision_and_false_hit(self):
        records = [rec(0.9, True), rec(0.8, False), rec(0.1, True)]
        self.assertAlmostEqual(analyze.precision(records, 0.5), 0.5)
        self.assertAlmostEqual(analyze.false_hit_b(records, 0.5), 0.5)

    def test_hit_at_budget_picks_threshold(self):
        records = [rec(0.9, True), rec(0.85, True), rec(0.8, True), rec(0.1, False)]
        hit = analyze.hit_at_budget(records, 0.0, "a")
        self.assertEqual(hit, 1.0)

    def test_bootstrap_bounds(self):
        records = [rec(0.9, True) if i % 4 == 0 else rec(0.1, False) for i in range(40)]
        point, lo, hi = analyze.bootstrap(records, analyze.auc, 200, seed=1)
        self.assertEqual(point, 1.0)
        self.assertLessEqual(lo, hi)
        self.assertGreaterEqual(lo, 0.0)


class TestLoad(unittest.TestCase):
    def test_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            gold = Path(tmp) / "g.jsonl"
            gold.write_text(json.dumps({"id": "a", "should_hit": True}) + "\n" + json.dumps({"id": "b", "should_hit": False}) + "\n")
            res = Path(tmp) / "r.jsonl"
            res.write_text(
                json.dumps({"id": "a", "state_mode": "m", "score": 0.9, "domain": "d"}) + "\n"
                + json.dumps({"id": "b", "state_mode": "m", "score": 0.1, "domain": "d"}) + "\n"
                + json.dumps({"id": "c", "state_mode": "m", "score": 0.5, "domain": "d"}) + "\n"
            )
            g = analyze.load_gold(gold)
            variants = analyze.load_results([res], g)
            self.assertEqual(len(variants["m"]), 2)
            self.assertTrue(variants["m"][0]["label"])


class TestLLMBaseline(unittest.TestCase):
    def test_to_probability(self):
        self.assertEqual(run_llm_baseline.to_probability({"probability": 0.7}), 0.7)
        self.assertEqual(run_llm_baseline.to_probability({"serve": True}), 1.0)
        self.assertEqual(run_llm_baseline.to_probability({"serve": "no"}), 0.0)
        self.assertEqual(run_llm_baseline.to_probability({"probability": 2}), 1.0)
        self.assertIsNone(run_llm_baseline.to_probability({}))

    def test_sanitize(self):
        self.assertEqual(run_llm_baseline.sanitize("Qwen3.5-397B-A17B"), "qwen3.5-397b-a17b")


if __name__ == "__main__":
    unittest.main()
