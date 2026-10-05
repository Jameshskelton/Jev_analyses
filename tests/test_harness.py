import json
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import baselines  # noqa: E402
import costs  # noqa: E402
import run_pilot  # noqa: E402

PAIRS = [
    {
        "id": "p1",
        "category": "paraphrase",
        "domain": "customer_support",
        "label_source": "native",
        "cached_query": "reset my password",
        "cached_response": "Go to settings and choose reset password.",
        "new_query": "how do i change my password",
        "should_hit": True,
    },
    {
        "id": "p2",
        "category": "entity_swap",
        "domain": "open_domain_qa",
        "label_source": "heuristic",
        "cached_query": "who directed inception",
        "cached_response": "christopher nolan",
        "new_query": "who directed interstellar",
        "should_hit": False,
    },
]


class TestRequestBuilder(unittest.TestCase):
    def test_json_query_and_response(self):
        request = run_pilot.build_request(PAIRS[0], "json_query_and_response", True, "typesafe-jev-1.13.0")
        self.assertEqual(request["model"], "typesafe-jev-1.13.0")
        self.assertEqual(set(request["state"]), {"cached_query", "cached_response", "new_query"})
        question = request["questions"][run_pilot.QUESTION_ID]
        self.assertEqual(question["type"], "noul")
        self.assertIn("`cached_response`", question["instructions"])
        self.assertIn("`new_query`", question["instructions"])
        self.assertEqual(set(question["criteria"]), {"true", "false"})

    def test_json_response_only_omits_query(self):
        request = run_pilot.build_request(PAIRS[0], "json_response_only", True, "m")
        self.assertEqual(set(request["state"]), {"cached_response", "new_query"})

    def test_plain_string(self):
        request = run_pilot.build_request(PAIRS[0], "plain_string", False, "m")
        self.assertIsInstance(request["state"], str)
        self.assertIn("CACHED RESPONSE:", request["state"])
        self.assertNotIn("criteria", request["questions"][run_pilot.QUESTION_ID])

    def test_deterministic(self):
        a = run_pilot.build_request(PAIRS[1], "json_query_and_response", True, "m")
        b = run_pilot.build_request(PAIRS[1], "json_query_and_response", True, "m")
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))


class TestScoring(unittest.TestCase):
    def test_auc_perfect_and_reversed(self):
        positive = [0.9, 0.8, 0.7]
        negative = [0.2, 0.1, 0.0]
        self.assertAlmostEqual(run_pilot.auc(positive, negative), 1.0)
        self.assertAlmostEqual(run_pilot.auc(negative, positive), 0.0)

    def test_auc_ties(self):
        self.assertAlmostEqual(run_pilot.auc([0.5], [0.5]), 0.5)

    def test_metrics_known(self):
        results = [
            run_pilot.Result(id="a", state_mode="m", category="c", domain="d", label_source="l",
                             cached_query="q", cached_response="r", new_query="n", should_hit=True, score=0.9,
                             predicted_hit=True, correct=True),
            run_pilot.Result(id="b", state_mode="m", category="c", domain="d", label_source="l",
                             cached_query="q", cached_response="r", new_query="n", should_hit=True, score=0.4,
                             predicted_hit=False, correct=False),
            run_pilot.Result(id="c", state_mode="m", category="c", domain="d", label_source="l",
                             cached_query="q", cached_response="r", new_query="n", should_hit=False, score=0.1,
                             predicted_hit=False, correct=True),
        ]
        m = run_pilot.metrics(results, 0.5)
        self.assertEqual(m["n_should_hit"], 2)
        self.assertEqual(m["hit_rate"], 0.5)
        self.assertEqual(m["false_hit_rate_b"], 0.0)
        self.assertEqual(m["accuracy"], 2 / 3)

    def test_operating_point_reachable(self):
        results = [
            run_pilot.Result(id="a", state_mode="m", category="c", domain="d", label_source="l",
                             cached_query="q", cached_response="r", new_query="n", should_hit=True, score=0.9),
            run_pilot.Result(id="b", state_mode="m", category="c", domain="d", label_source="l",
                             cached_query="q", cached_response="r", new_query="n", should_hit=False, score=0.8),
        ]
        points = run_pilot.operating_points(results, (0.0,))
        self.assertEqual(points["0%"]["threshold"], 0.9)
        self.assertEqual(points["0%"]["hit_rate"], 1.0)

    def test_operating_point_unreachable(self):
        results = [
            run_pilot.Result(id="a", state_mode="m", category="c", domain="d", label_source="l",
                             cached_query="q", cached_response="r", new_query="n", should_hit=True, score=0.2),
            run_pilot.Result(id="b", state_mode="m", category="c", domain="d", label_source="l",
                             cached_query="q", cached_response="r", new_query="n", should_hit=False, score=0.95),
        ]
        self.assertIsNone(run_pilot.operating_points(results, (0.0,))["0%"])

    def test_cost_model(self):
        self.assertAlmostEqual(costs.jev_cost(1_000_000), 0.042)
        self.assertAlmostEqual(costs.embedding_cost("gte-large-en-v1.5", 1_000_000), 0.09)
        self.assertAlmostEqual(costs.embedding_cost("bge-m3", 1_000_000), 0.02)


class TestJevParsing(unittest.TestCase):
    def test_run_task_parses_answer_and_cost(self):
        canned = (
            {"answers": {run_pilot.QUESTION_ID: {"type": "noul", "noul": 0.87}},
             "model": "jev-1.13.0", "usage": {"input_tokens": 300, "output_tokens": 20}},
            1,
            123.0,
        )
        args = types.SimpleNamespace(
            url="http://x", headers={}, timeout=1.0, max_retries=1,
            use_criteria=True, model="typesafe-jev-1.13.0", threshold=0.5,
        )
        with mock.patch.object(run_pilot, "post_with_retries", return_value=canned):
            result = run_pilot.run_task(None, PAIRS[0], "json_query_and_response", args)
        self.assertEqual(result.score, 0.87)
        self.assertTrue(result.predicted_hit)
        self.assertTrue(result.correct)
        self.assertIsNotNone(result.timestamp)
        self.assertAlmostEqual(result.cost_usd, 300 * 0.042 / 1_000_000)
        self.assertEqual(result.model, "jev-1.13.0")


class TestBaselineDeterminism(unittest.TestCase):
    def make_args(self):
        return types.SimpleNamespace(
            baselines="tfidf",
            baseline_compare="both",
            do_models=[],
            openai_models=[],
            st_models=[],
            do_key=None,
            threshold=0.5,
        )

    def test_tfidf_reproducible(self):
        first, variants_first = run_pilot.build_baseline_results(PAIRS, self.make_args())
        second, variants_second = run_pilot.build_baseline_results(PAIRS, self.make_args())
        self.assertEqual(variants_first, variants_second)
        self.assertEqual(
            [(r.id, r.state_mode, round(r.score, 12)) for r in first],
            [(r.id, r.state_mode, round(r.score, 12)) for r in second],
        )

    def test_embedding_run_shape(self):
        embedder = baselines.TfidfEmbedder([p["new_query"] for p in PAIRS])
        run = baselines.score_pairs(PAIRS, embedder, "query_to_query")
        self.assertEqual(len(run.scores), len(PAIRS))
        self.assertGreaterEqual(run.batch_latency_ms, 0.0)


class TestCLI(unittest.TestCase):
    def test_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            pairs = Path(tmp) / "pairs.jsonl"
            pairs.write_text(json.dumps({
                "id": "p1", "category": "paraphrase", "domain": "customer_support",
                "cached_query": "reset my password", "cached_response": "Go to settings.",
                "new_query": "how do I change my password", "heuristic_label": True,
            }) + "\n")
            result = subprocess.run(
                [sys.executable, str(ROOT / "run_pilot.py"), "--pairs", str(pairs),
                 "--dry-run", "--baselines", "all"],
                capture_output=True, text=True, cwd=str(ROOT),
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("pairs=", result.stdout)

    def test_hash_imports(self):
        for module in ("run_pilot", "baselines", "costs"):
            self.assertIn(module, sys.modules)


if __name__ == "__main__":
    unittest.main()
