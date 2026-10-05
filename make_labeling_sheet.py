#!/usr/bin/env python3
"""Build a stratified human-labeling sheet from the candidate pool.

Produces:
    labeling_sheet.csv   rows to fill in (columns: ..., label, confidence, notes)
    labeling_key.json    rubric + row->sample mapping + full content (for scoring)
    sample.jsonl         the unique sampled pairs (for run_judge.py)

A single annotator has no second rater, so a fraction of items are duplicated
(blinded, shuffled) to measure intra-annotator test-retest consistency. The key
records which rows are duplicates so agreement.py can score them.

Usage:
    python make_labeling_sheet.py --pool pool.jsonl --n 500 --duplicates 50
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
from collections import defaultdict
from pathlib import Path
from typing import Any

RUBRIC = (
    "You are labeling whether a semantic cache would be correct to serve. "
    "For each row you see a cached_query, the cached_response that was stored, and a new_query. "
    "Decide: would serving cached_response as the answer to new_query be CORRECT? "
    "Enter 'hit' if the cached response fully and correctly answers the new query, or 'miss' if it "
    "does not (different question, wrong entity or number, negation, broader/narrower scope, partial "
    "answer, stale, or an unrelated/refusal response). If unsure, still choose one and note why. "
    "Some items repeat; label each row independently from its text alone."
)


def allocate(strata: dict[Any, list[Any]], n: int, min_per: int = 3) -> dict[Any, int]:
    total = sum(len(v) for v in strata.values())
    quotas: dict[Any, float] = {key: n * len(value) / total for key, value in strata.items()}
    alloc: dict[Any, int] = {key: min(len(strata[key]), max(min_per, int(math.floor(quota)))) for key, quota in quotas.items()}
    while sum(alloc.values()) > n:
        key = max(alloc, key=lambda k: alloc[k] - quotas[k])
        if alloc[key] > 0:
            alloc[key] -= 1
    order = sorted(quotas, key=lambda k: quotas[k] - alloc[k], reverse=True)
    while sum(alloc.values()) < n:
        progressed = False
        for key in order:
            if alloc[key] < len(strata[key]):
                alloc[key] += 1
                progressed = True
                if sum(alloc.values()) == n:
                    break
        if not progressed:
            break
    return alloc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pool", type=Path, default=Path("pool.jsonl"))
    parser.add_argument("--n", type=int, default=500, help="unique pairs to sample")
    parser.add_argument("--duplicates", type=int, default=50, help="blinded repeat rows for test-retest")
    parser.add_argument("--sheet", type=Path, default=Path("labeling_sheet.csv"))
    parser.add_argument("--key", type=Path, default=Path("labeling_key.json"))
    parser.add_argument("--sample", type=Path, default=Path("sample.jsonl"))
    parser.add_argument("--seed", type=int, default=13)
    args = parser.parse_args()
    rng = random.Random(args.seed)

    pool = [json.loads(line) for line in args.pool.read_text().splitlines() if line.strip()]
    if not pool:
        raise SystemExit(f"empty pool: {args.pool}")

    strata: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for pair in pool:
        strata[(pair["split"], pair["domain"], pair["category"])].append(pair)

    alloc = allocate(strata, args.n)
    sampled: list[dict[str, Any]] = []
    for key in sorted(strata):
        chosen = rng.sample(strata[key], min(alloc[key], len(strata[key])))
        sampled.extend(chosen)

    samples: dict[str, dict[str, Any]] = {}
    for index, pair in enumerate(sampled):
        sample_id = f"s_{index:04d}"
        samples[sample_id] = pair
        pair["_sample_id"] = sample_id

    with args.sample.open("w") as handle:
        for pair in sampled:
            handle.write(json.dumps({k: v for k, v in pair.items() if not k.startswith("_")}) + "\n")

    duplicate_ids = rng.sample(list(samples), min(args.duplicates, len(samples)))
    rows = [(sid, False) for sid in samples] + [(sid, True) for sid in duplicate_ids]
    rng.shuffle(rows)

    key_rows: dict[str, dict[str, Any]] = {}
    with args.sheet.open("w", newline="") as handle:
        writer = csv.writer(handle, quoting=csv.QUOTE_ALL)
        writer.writerow(["row_id", "domain", "category", "cached_query", "cached_response", "new_query", "label", "confidence", "notes"])
        for index, (sample_id, is_duplicate) in enumerate(rows):
            row_id = f"r_{index:05d}"
            pair = samples[sample_id]
            key_rows[row_id] = {"sample_id": sample_id, "is_duplicate": is_duplicate, "split": pair["split"]}
            writer.writerow([
                row_id, pair["domain"], pair["category"], pair["cached_query"],
                pair["cached_response"], pair["new_query"], "", "", "",
            ])

    key = {
        "rubric": RUBRIC,
        "n_unique": len(samples),
        "n_rows": len(rows),
        "n_duplicates": len(duplicate_ids),
        "rows": key_rows,
        "samples": samples,
    }
    args.key.write_text(json.dumps(key, indent=2))

    print(f"wrote {args.sheet} ({len(rows)} rows: {len(samples)} unique + {len(duplicate_ids)} duplicates)")
    print(f"wrote {args.key} and {args.sample}")
    print("\nRUBRIC:\n" + RUBRIC)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
