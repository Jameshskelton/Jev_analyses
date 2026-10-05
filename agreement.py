#!/usr/bin/env python3
"""Score Phase 2 labeling quality.

With one annotator there is no inter-annotator agreement, so this reports:
  1. Intra-annotator consistency on the blinded duplicate rows (test-retest).
  2. Human-vs-judge agreement, to decide whether the judge can label the rest.
  3. Heuristic-vs-human agreement, to quantify how noisy the pilot labels were.

Usage:
    python agreement.py --sheet labeling_sheet.csv --key labeling_key.json \
        --judge judge_labels.jsonl --pool pool.jsonl
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

POSITIVE = {"hit", "yes", "y", "true", "t", "1", "correct", "answer", "serve"}
NEGATIVE = {"miss", "no", "n", "false", "f", "0", "incorrect", "not_answer", "reject"}


def parse_label(value: str) -> bool | None:
    if value is None:
        return None
    lowered = value.strip().lower()
    if lowered in POSITIVE:
        return True
    if lowered in NEGATIVE:
        return False
    return None


def kappa(pairs: list[tuple[bool, bool]]) -> float | None:
    if not pairs:
        return None
    n = len(pairs)
    observed = sum(1 for a, b in pairs if a == b) / n
    rate_a = sum(1 for a, _ in pairs if a) / n
    rate_b = sum(1 for _, b in pairs if b) / n
    expected = rate_a * rate_b + (1 - rate_a) * (1 - rate_b)
    if expected >= 1.0:
        return 1.0 if observed == 1.0 else 0.0
    return (observed - expected) / (1 - expected)


def rate(pairs: list[tuple[bool, bool]]) -> float | None:
    if not pairs:
        return None
    return sum(1 for a, b in pairs if a == b) / len(pairs)


def fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def confusion(pairs: list[tuple[bool, bool]]) -> dict[str, int]:
    table = {"hit_hit": 0, "hit_miss": 0, "miss_hit": 0, "miss_miss": 0}
    for a, b in pairs:
        table[("hit" if a else "miss") + "_" + ("hit" if b else "miss")] += 1
    return table


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sheet", type=Path, default=Path("labeling_sheet.csv"))
    parser.add_argument("--key", type=Path, default=Path("labeling_key.json"))
    parser.add_argument("--judge", type=Path, default=None)
    parser.add_argument("--pool", type=Path, default=None, help="pool.jsonl (used only if key lacks heuristic_label)")
    parser.add_argument("--out", type=Path, default=Path("agreement_summary.json"))
    args = parser.parse_args()

    key = json.loads(args.key.read_text())
    rows_meta = key["rows"]
    samples = key["samples"]
    split_by_sample = {meta["sample_id"]: meta["split"] for meta in rows_meta.values()}

    labels_by_sample: dict[str, list[bool]] = defaultdict(list)
    with args.sheet.open(newline="") as handle:
        for row in csv.DictReader(handle):
            label = parse_label(row.get("label", ""))
            meta = rows_meta.get(row["row_id"])
            if label is None or meta is None:
                continue
            labels_by_sample[meta["sample_id"]].append(label)

    human: dict[str, bool] = {sid: values[0] for sid, values in labels_by_sample.items()}
    print(f"human labels: {len(human)} / {key['n_unique']} unique samples "
          f"({len(labels_by_sample)} coverage, {key['n_duplicates']} duplicate rows expected)")

    intra = [(values[0], values[1]) for values in labels_by_sample.values() if len(values) >= 2]
    print("\nintra-annotator (test-retest on duplicates):")
    print(f"  n={len(intra)}  agreement={fmt(rate(intra))}  kappa={fmt(kappa(intra))}")
    if intra:
        print(f"  {confusion(intra)}")

    summary: dict[str, Any] = {
        "n_human": len(human),
        "intra_annotator": {"n": len(intra), "agreement": rate(intra), "kappa": kappa(intra), "confusion": confusion(intra)},
    }

    judge: dict[str, bool] = {}
    if args.judge:
        for line in args.judge.read_text().splitlines():
            if line.strip():
                record = json.loads(line)
                if record.get("should_hit") is not None:
                    judge[record["id"]] = bool(record["should_hit"])

    if judge:
        pairs: list[tuple[bool, bool]] = []
        by_category: dict[str, list[tuple[bool, bool]]] = defaultdict(list)
        by_split: dict[str, list[tuple[bool, bool]]] = defaultdict(list)
        for sample_id, label in human.items():
            pair = samples.get(sample_id, {})
            judge_label = judge.get(pair.get("id"))
            if judge_label is None:
                continue
            pairs.append((label, judge_label))
            by_category[pair.get("category", "?")].append((label, judge_label))
            by_split[split_by_sample.get(sample_id, "?")].append((label, judge_label))
        print("\nhuman vs judge:")
        print(f"  n={len(pairs)}  agreement={fmt(rate(pairs))}  kappa={fmt(kappa(pairs))}")
        print(f"  confusion {confusion(pairs)}")
        print(f"  {'category':<20} {'n':>4} {'agree':>6} {'kappa':>6}")
        for category, cell in sorted(by_category.items()):
            print(f"  {category:<20} {len(cell):>4} {fmt(rate(cell)):>6} {fmt(kappa(cell)):>6}")
        print(f"  {'split':<20} {'n':>4} {'agree':>6} {'kappa':>6}")
        for split, cell in sorted(by_split.items()):
            print(f"  {split:<20} {len(cell):>4} {fmt(rate(cell)):>6} {fmt(kappa(cell)):>6}")
        summary["human_vs_judge"] = {
            "n": len(pairs), "agreement": rate(pairs), "kappa": kappa(pairs),
            "confusion": confusion(pairs),
            "by_category": {k: {"n": len(v), "agreement": rate(v), "kappa": kappa(v)} for k, v in by_category.items()},
            "by_split": {k: {"n": len(v), "agreement": rate(v), "kappa": kappa(v)} for k, v in by_split.items()},
        }

    heuristic = [(label, bool(samples[sid].get("heuristic_label"))) for sid, label in human.items() if "heuristic_label" in samples.get(sid, {})]
    if heuristic:
        print("\nheuristic vs human (how noisy the pilot labels were):")
        print(f"  n={len(heuristic)}  agreement={fmt(rate(heuristic))}  kappa={fmt(kappa(heuristic))}")
        print(f"  confusion {confusion(heuristic)}")
        summary["heuristic_vs_human"] = {"n": len(heuristic), "agreement": rate(heuristic), "kappa": kappa(heuristic), "confusion": confusion(heuristic)}

    args.out.write_text(json.dumps(summary, indent=2))
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
