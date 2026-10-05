#!/usr/bin/env python3
"""Build frozen gold-label sets for the Jev-vs-cosine comparison.

Two consistent policies, so the headline can be reported with a sensitivity
check rather than depending on one contested labeling choice:

  strict   A hit only if the cached response actually contains the requested
           answer. Generic acknowledgments/templates and same-topic-different-
           fact pairs are misses. Source: the independent judge's labels.

  lenient  Same intent/scope, or a coincident correct short answer, counts as a
           hit. Source: the assistant pre-labels.

Both are mapped to the pool pair id so run_pilot.py --labels can consume them.

Usage:
    python make_gold_sets.py \
        --judge judge_labels_20261002T172608Z.jsonl \
        --prelabels assistant_prelabels.jsonl \
        --key labeling_key.json \
        --strict gold_strict.jsonl --lenient gold_lenient.jsonl
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

POSITIVE = {"hit", "yes", "y", "true", "t", "1", "correct"}
NEGATIVE = {"miss", "no", "n", "false", "f", "0", "incorrect"}


def to_bool(value) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in POSITIVE:
            return True
        if lowered in NEGATIVE:
            return False
    return None


def write(path: Path, records: list[dict]) -> None:
    records.sort(key=lambda r: r["id"])
    with path.open("w") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")
    hits = sum(1 for r in records if r["should_hit"])
    print(f"wrote {len(records)} labels to {path} (hit={hits}, miss={len(records) - hits})")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--judge", type=Path, required=True)
    parser.add_argument("--prelabels", type=Path, required=True)
    parser.add_argument("--key", type=Path, default=Path("labeling_key.json"))
    parser.add_argument("--strict", type=Path, default=Path("gold_strict.jsonl"))
    parser.add_argument("--lenient", type=Path, default=Path("gold_lenient.jsonl"))
    args = parser.parse_args()

    key = json.loads(args.key.read_text())
    rows = key["rows"]
    samples = key["samples"]
    row_to_pair = {row_id: samples[meta["sample_id"]]["id"] for row_id, meta in rows.items()}

    strict: list[dict] = []
    judge_model = None
    for line in args.judge.read_text().splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        label = to_bool(record.get("should_hit"))
        if label is None:
            label = to_bool(record.get("label"))
        if label is None:
            continue
        judge_model = record.get("model", judge_model)
        strict.append({"id": record["id"], "should_hit": label, "source": f"judge:{judge_model}"})

    lenient: list[dict] = []
    for line in args.prelabels.read_text().splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        pair_id = row_to_pair.get(record["row_id"])
        label = to_bool(record.get("label"))
        if pair_id is None or label is None:
            continue
        if record.get("is_duplicate"):
            continue
        lenient.append({"id": pair_id, "should_hit": label, "source": "assistant-prelabel"})

    dedup: dict[str, dict] = {}
    for record in lenient:
        dedup[record["id"]] = record
    lenient = list(dedup.values())

    write(args.strict, strict)
    write(args.lenient, lenient)

    strict_by_id = {r["id"]: r["should_hit"] for r in strict}
    lenient_by_id = {r["id"]: r["should_hit"] for r in lenient}
    shared = set(strict_by_id) & set(lenient_by_id)
    agree = sum(1 for i in shared if strict_by_id[i] == lenient_by_id[i])
    print(f"agreement between policies on {len(shared)} shared pairs: {agree}/{len(shared)} = {agree / len(shared):.1%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
