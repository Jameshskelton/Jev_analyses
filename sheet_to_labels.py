#!/usr/bin/env python3
"""Convert a filled labeling sheet into a gold-labels JSONL for run_pilot.py.

Maps each row to its pool pair id via the labeling key. When a sample was
blinded-duplicated and the two answers disagree, the sample is flagged and
resolved by majority (falling back to the first, with a warning).

Usage:
    python sheet_to_labels.py --sheet labeling_sheet.csv --key labeling_key.json \
        --out gold_labels.jsonl
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path

POSITIVE = {"hit", "yes", "y", "true", "t", "1", "correct", "answer", "serve"}
NEGATIVE = {"miss", "no", "n", "false", "f", "0", "incorrect", "not_answer", "reject"}


def parse_label(value: str) -> bool | None:
    lowered = (value or "").strip().lower()
    if lowered in POSITIVE:
        return True
    if lowered in NEGATIVE:
        return False
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sheet", type=Path, default=Path("labeling_sheet.csv"))
    parser.add_argument("--key", type=Path, default=Path("labeling_key.json"))
    parser.add_argument("--out", type=Path, default=Path("gold_labels.jsonl"))
    args = parser.parse_args()

    key = json.loads(args.key.read_text())
    rows = key["rows"]
    samples = key["samples"]

    by_sample: dict[str, list[bool]] = defaultdict(list)
    with args.sheet.open(newline="") as handle:
        for row in csv.DictReader(handle):
            label = parse_label(row.get("label", ""))
            meta = rows.get(row["row_id"])
            if label is None or meta is None:
                continue
            by_sample[meta["sample_id"]].append(label)

    conflicts = []
    records = []
    for sample_id, values in by_sample.items():
        pair = samples[sample_id]
        if len(values) >= 2 and values[0] != values[1]:
            conflicts.append(sample_id)
        gold = values[0] if values[0] == values[-1] else max(set(values), key=values.count)
        records.append({"id": pair["id"], "should_hit": gold, "source": "human", "sample_id": sample_id})

    records.sort(key=lambda r: r["id"])
    with args.out.open("w") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")

    print(f"wrote {len(records)} gold labels to {args.out}")
    hits = sum(1 for r in records if r["should_hit"])
    print(f"  hit={hits} miss={len(records) - hits}")
    if conflicts:
        print(f"  {len(conflicts)} duplicate conflicts (used majority/first): {conflicts[:10]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
