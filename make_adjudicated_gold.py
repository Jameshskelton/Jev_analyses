#!/usr/bin/env python3
"""Build the adjudicated gold set under the frozen policy: B hit, A miss.

Rules (applied to the 500-pair sample):
    same_answer   -> take the assistant label (the short answer is correct for
                     the new question, so serve it; this also keeps the one
                     factually-wrong case a miss).
    paraphrase    -> take the judge label (strict: a same-intent acknowledgment
                     that does not contain the answer is a miss).
    adversarial / agreement -> take the judge label (both labelers agree).

One manual override: r_00211, where the stored reply explicitly declines help,
is forced to miss.

Usage:
    python make_adjudicated_gold.py \
        --key labeling_key.json --prelabels assistant_prelabels.jsonl \
        --judge judge_labels_20261002T172608Z.jsonl --out gold_adjudicated.jsonl
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def to_bool(value) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"hit", "yes", "y", "true", "1", "correct"}:
            return True
        if lowered in {"miss", "no", "n", "false", "0", "incorrect"}:
            return False
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--key", type=Path, default=Path("labeling_key.json"))
    parser.add_argument("--prelabels", type=Path, default=Path("assistant_prelabels.jsonl"))
    parser.add_argument("--judge", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=Path("gold_adjudicated.jsonl"))
    parser.add_argument("--force-miss", default="r_00211", help="comma-separated row_ids forced to miss")
    args = parser.parse_args()

    key = json.loads(args.key.read_text())
    rows = key["rows"]
    samples = key["samples"]

    pre = {json.loads(line)["row_id"]: to_bool(json.loads(line).get("label")) for line in args.prelabels.read_text().splitlines() if line.strip()}
    judge = {}
    for line in args.judge.read_text().splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        label = to_bool(record.get("should_hit")) if record.get("should_hit") is not None else to_bool(record.get("label"))
        judge[record["id"]] = label

    force_miss = {r.strip() for r in args.force_miss.split(",") if r.strip()}
    seen: set[str] = set()
    records = []
    for row_id, meta in rows.items():
        sample_id = meta["sample_id"]
        if sample_id in seen:
            continue
        seen.add(sample_id)
        pair = samples[sample_id]
        category = pair["category"]
        assistant = pre.get(row_id)
        judged = judge.get(pair["id"])

        if category == "same_answer" and assistant is not None:
            label = assistant
        elif judged is not None:
            label = judged
        else:
            label = assistant

        if row_id in force_miss:
            label = False
        if label is None:
            continue
        records.append({"id": pair["id"], "should_hit": bool(label), "source": "adjudicated:B-hit,A-miss"})

    records.sort(key=lambda r: r["id"])
    with args.out.open("w") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")
    hits = sum(1 for r in records if r["should_hit"])
    print(f"wrote {len(records)} labels to {args.out} (hit={hits}, miss={len(records) - hits})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
