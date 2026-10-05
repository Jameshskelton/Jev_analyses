import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import compare_runs  # noqa: E402


def write(path: Path, rows):
    with path.open("w") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def row(identifier, mode, score):
    return {"id": identifier, "state_mode": mode, "score": score}


class TestCompareRuns(unittest.TestCase):
    def test_summarize(self):
        stats = compare_runs.summarize([0.0, 0.1, -0.2], exact=1, flips=1, total=3)
        self.assertEqual(stats["pairs"], 3)
        self.assertEqual(stats["exactly_equal"], 1)
        self.assertEqual(stats["changed"], 2)
        self.assertAlmostEqual(stats["max_abs_delta"], 0.2)
        self.assertEqual(stats["sign_flips_at_threshold"], 1)

    def test_load_and_join(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = Path(tmp) / "a.jsonl"
            b = Path(tmp) / "b.jsonl"
            write(a, [row("x", "m", 0.9), row("y", "m", 0.2), row("z", "m", None)])
            write(b, [row("x", "m", 0.8), row("y", "m", 0.2)])
            left = compare_runs.load_results(a)
            right = compare_runs.load_results(b)
            self.assertEqual(set(left), {("x", "m"), ("y", "m")})
            self.assertEqual(set(right), {("x", "m"), ("y", "m")})


if __name__ == "__main__":
    unittest.main()
