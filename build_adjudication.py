#!/usr/bin/env python3
"""Build a sanitized adjudication view of the label-policy disagreements.

Takes the pairs where the two independent labelers (assistant pre-labels vs the
judge) disagree, masks abusive language, groups them by the two dispute
patterns, and writes a fillable CSV so a human can rule on the policy.

Patterns:
    A  paraphrase: assistant says hit, judge says miss -- typically a templated
       support reply that does not contain the answer. Policy question: does a
       same-intent acknowledgment count as answering?
    B  same_answer: same short answer, different question. Policy question:
       does a coincidentally-correct short answer count as a hit?

Usage:
    python build_adjudication.py --key labeling_key.json \
        --prelabels assistant_prelabels.jsonl --judge judge_labels_<ts>.jsonl \
        --out adjudication.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path

PROFANITY = re.compile(r"\b(fuck\w*|shit\w*|bloody|goddamn|god\s*damn|damn|bitch\w*|asshole\w*|crap\w*)\b", re.IGNORECASE)


def sanitize(text: str) -> str:
    return PROFANITY.sub("[expletive]", text or "")


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


def pattern_for(category: str) -> str:
    if category == "paraphrase":
        return "A: template/non-answer (same intent)"
    if category == "same_answer":
        return "B: same-answer different-question"
    return "C: other"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--key", type=Path, default=Path("labeling_key.json"))
    parser.add_argument("--prelabels", type=Path, default=Path("assistant_prelabels.jsonl"))
    parser.add_argument("--judge", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=Path("adjudication.csv"))
    args = parser.parse_args()

    key = json.loads(args.key.read_text())
    rows = key["rows"]
    samples = key["samples"]

    pre = {json.loads(line)["row_id"]: json.loads(line) for line in args.prelabels.read_text().splitlines() if line.strip()}
    judge = {}
    for line in args.judge.read_text().splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        judge[record["id"]] = to_bool(record.get("should_hit")) if record.get("should_hit") is not None else to_bool(record.get("label"))

    seen: set[str] = set()
    records = []
    for row_id, meta in rows.items():
        sample_id = meta["sample_id"]
        if sample_id in seen:
            continue
        seen.add(sample_id)
        pair = samples[sample_id]
        assistant = to_bool(pre[row_id].get("label")) if row_id in pre else None
        judged = judge.get(pair["id"])
        if assistant is None or judged is None or assistant == judged:
            continue
        records.append({
            "row_id": row_id,
            "pattern": pattern_for(pair["category"]),
            "domain": pair["domain"],
            "category": pair["category"],
            "assistant": "hit" if assistant else "miss",
            "judge": "hit" if judged else "miss",
            "cached_query": sanitize(pair["cached_query"]),
            "cached_response": sanitize(pair["cached_response"]),
            "new_query": sanitize(pair["new_query"]),
            "decision": "",
            "notes": "",
        })

    order = {"A: template/non-answer (same intent)": 0, "B: same-answer different-question": 1, "C: other": 2}
    records.sort(key=lambda r: (order[r["pattern"]], r["row_id"]))

    with args.out.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["row_id", "pattern", "domain", "category", "assistant", "judge",
                                                     "cached_query", "cached_response", "new_query", "decision", "notes"],
                                quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(records)

    counts: dict[str, int] = {}
    direction: dict[str, int] = {}
    for record in records:
        counts[record["pattern"]] = counts.get(record["pattern"], 0) + 1
        key2 = f"{record['assistant']}->{record['judge']}"
        direction[key2] = direction.get(key2, 0) + 1

    print(f"wrote {len(records)} disagreements to {args.out}")
    for pattern, count in sorted(counts.items()):
        print(f"  {pattern:<40} {count}")
    print(f"  directions: {direction}")
    print("\nRule per pattern (fill the 'decision' column to override individual rows):")
    print("  A: 'miss' = a same-intent acknowledgment that lacks the answer is NOT a hit (strict);")
    print("     'hit'  = same intent is enough for the cache (lenient).")
    print("  B: 'miss' = coincidentally-equal short answer is NOT a hit (questions differ);")
    print("     'hit'  = the response contains the correct answer, so serve it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
