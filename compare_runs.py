#!/usr/bin/env python3
"""Compare two run result files (or summaries) to quantify hosted-model drift.

Joins two result JSONL files on (id, state_mode) and reports how much each
score moved between runs. Use it to answer: is a hosted model's output stable
enough that the experiment's per-pair scores are reproducible?

Usage:
    python compare_runs.py --a pilot_results_run1.jsonl --b pilot_results_run2.jsonl
    python compare_runs.py --a results1.jsonl --b results2.jsonl --threshold 0.05 --state-mode json_query_and_response
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def load_results(path: Path) -> dict[tuple[str, str], float]:
    scored: dict[tuple[str, str], float] = {}
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("score") is None:
            continue
        scored[(row["id"], row["state_mode"])] = float(row["score"])
    return scored


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(len(ordered) * fraction))
    return ordered[index]


def summarize(deltas: list[float], exact: int, flips: int, total: int) -> dict[str, float]:
    absolute = [abs(delta) for delta in deltas]
    return {
        "pairs": total,
        "exactly_equal": exact,
        "changed": total - exact,
        "changed_fraction": (total - exact) / total if total else 0.0,
        "mean_abs_delta": sum(absolute) / len(absolute) if absolute else 0.0,
        "median_abs_delta": percentile(absolute, 0.5),
        "p95_abs_delta": percentile(absolute, 0.95),
        "max_abs_delta": max(absolute) if absolute else 0.0,
        "sign_flips_at_threshold": flips,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--a", type=Path, required=True)
    parser.add_argument("--b", type=Path, required=True)
    parser.add_argument("--threshold", type=float, default=0.5, help="threshold at which to count decision flips")
    parser.add_argument("--state-mode", default=None, help="restrict to one state_mode")
    args = parser.parse_args()

    a = load_results(args.a)
    b = load_results(args.b)
    keys = sorted(set(a) & set(b))
    if not keys:
        raise SystemExit("no overlapping (id, state_mode) keys; are these the same run/pairs?")

    deltas = [b[key] - a[key] for key in keys]
    exact = sum(1 for delta in deltas if abs(delta) <= 1e-9)
    flips = sum(1 for key in keys if (a[key] >= args.threshold) != (b[key] >= args.threshold))
    overall = summarize(deltas, exact, flips, len(keys))

    print(f"A: {args.a}  ({len(a)} scored rows)")
    print(f"B: {args.b}  ({len(b)} scored rows)")
    print(f"overlap: {len(keys)} (id, state_mode) pairs  |  threshold={args.threshold}")
    print(
        f"  exact={overall['exactly_equal']} ({1 - overall['changed_fraction']:.1%})  "
        f"changed={overall['changed']}  flips={overall['sign_flips_at_threshold']}"
    )
    print(
        f"  |delta| mean={overall['mean_abs_delta']:.6f}  median={overall['median_abs_delta']:.6f}  "
        f"p95={overall['p95_abs_delta']:.6f}  max={overall['max_abs_delta']:.6f}"
    )

    modes = sorted({key[1] for key in keys})
    print(f"\n  {'state_mode':<36} {'n':>4} {'exact':>6} {'flips':>6} {'mean|d|':>9} {'max|d|':>9}")
    for mode in modes:
        if args.state_mode and mode != args.state_mode:
            continue
        mode_keys = [key for key in keys if key[1] == mode]
        mode_deltas = [b[key] - a[key] for key in mode_keys]
        mode_exact = sum(1 for delta in mode_deltas if abs(delta) <= 1e-9)
        mode_flips = sum(1 for key in mode_keys if (a[key] >= args.threshold) != (b[key] >= args.threshold))
        mode_stats = summarize(mode_deltas, mode_exact, mode_flips, len(mode_keys))
        print(
            f"  {mode:<36} {mode_stats['pairs']:>4} {mode_stats['exactly_equal']:>6} "
            f"{mode_stats['sign_flips_at_threshold']:>6} {mode_stats['mean_abs_delta']:>9.5f} "
            f"{mode_stats['max_abs_delta']:>9.5f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
