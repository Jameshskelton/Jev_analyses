#!/usr/bin/env python3
"""Build a larger candidate pair pool from HuggingFace datasets for Phase 2.

Uses real records only (no generated text). Volume comes from sampling many
pairs within/across intents and answer groups rather than one pair per cluster.

Output fields per pair:
    id, category, domain, source, split, cached_query, cached_response,
    new_query, heuristic_label, heuristic_source, notes

heuristic_label is the pilot's automatic guess; it is NOT ground truth. It is
kept so human/judge labels can be compared against it to quantify label noise.

Splits are leakage-safe: clustered on (source, answer) before hashing, so no
cluster straddles dev and test.

Usage:
    python build_pool.py --out pool.jsonl --seed 13
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import random
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from build_pilot_pairs import (
    BITEXT_ID,
    PLACEHOLDER,
    TRIVIA_ID,
    intent_tokens,
    jaccard,
    norm,
    stream_rows,
)

WORD = re.compile(r"[a-z0-9]+")

SUPPORT_COUNTS = {"paraphrase": 1200, "near_miss_intent": 300, "unrelated": 300}
QA_COUNTS = {"same_answer": 400, "entity_swap": 500, "number_change": 300}


def collect_support(max_per_intent: int = 400, max_rows: int = 60000) -> dict[str, list[tuple[str, str]]]:
    by_intent: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for row in stream_rows(BITEXT_ID, None, max_rows):
        instruction = (row.get("instruction") or "").strip()
        response = (row.get("response") or "").strip()
        intent = (row.get("intent") or "").strip()
        if not intent or not (4 <= len(instruction) <= 300) or not (3 <= len(response) <= 400):
            continue
        if PLACEHOLDER.search(response):
            continue
        bucket = by_intent[intent]
        if len(bucket) < max_per_intent:
            bucket.append((instruction, response))
        if len(by_intent) >= 27 and all(len(v) >= max_per_intent for v in by_intent.values()):
            break
    return dict(by_intent)


def collect_qa(limit: int = 4000) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    seen: set[str] = set()
    for row in stream_rows(TRIVIA_ID, "rc.nocontext", 400000):
        question = (row.get("question") or "").strip()
        answer = row.get("answer") or {}
        value = (answer.get("normalized_value") or answer.get("value") or "").strip() if isinstance(answer, dict) else str(answer).strip()
        if not question or not value or len(question) > 200:
            continue
        if len(norm(value).split()) > 4:
            continue
        key = norm(question)
        if key in seen:
            continue
        seen.add(key)
        rows.append((question, value))
        if len(rows) >= limit:
            break
    return rows


def add(pairs: list[dict[str, Any]], seen: set[tuple[str, str, str]], pair: dict[str, Any]) -> bool:
    key = (norm(pair["cached_query"]), norm(pair["new_query"]), norm(pair["cached_response"]))
    if key in seen:
        return False
    seen.add(key)
    pairs.append(pair)
    return True


def build_support(by_intent: dict[str, list[tuple[str, str]]], rng: random.Random, pairs: list[dict[str, Any]], seen: set) -> None:
    record, counts = make_add(pairs, seen)
    intents = sorted(by_intent)
    eligible = [i for i in intents if len(by_intent[i]) >= 2]

    attempts = 0
    while counts["paraphrase"] < SUPPORT_COUNTS["paraphrase"] and attempts < 20000:
        attempts += 1
        intent = rng.choice(eligible)
        first, second = rng.sample(by_intent[intent], 2)
        record({
            "category": "paraphrase", "domain": "customer_support",
            "source": f"hf:{BITEXT_ID}:intent={intent}",
            "cached_query": first[0], "cached_response": first[1], "new_query": second[0],
            "heuristic_label": True, "heuristic_source": "heuristic:intent_match",
            "notes": "same intent, sampled phrasing pair",
        })

    near = [(a, b) for a, b in itertools.combinations(intents, 2) if intent_tokens(a) & intent_tokens(b)]
    rng.shuffle(near)
    for a, b in near:
        if counts["near_miss_intent"] >= SUPPORT_COUNTS["near_miss_intent"]:
            break
        first = rng.choice(by_intent[a])
        second = rng.choice(by_intent[b])
        record({
            "category": "near_miss_intent", "domain": "customer_support",
            "source": f"hf:{BITEXT_ID}:intent={a}->{b}",
            "cached_query": first[0], "cached_response": first[1], "new_query": second[0],
            "heuristic_label": False, "heuristic_source": "heuristic:intent_mismatch",
            "notes": f"shared intent token {sorted(intent_tokens(a) & intent_tokens(b))}",
        })

    far = [(a, b) for a, b in itertools.combinations(intents, 2) if not (intent_tokens(a) & intent_tokens(b))]
    rng.shuffle(far)
    for a, b in far:
        if counts["unrelated"] >= SUPPORT_COUNTS["unrelated"]:
            break
        first = rng.choice(by_intent[a])
        second = rng.choice(by_intent[b])
        record({
            "category": "unrelated", "domain": "customer_support",
            "source": f"hf:{BITEXT_ID}:intent={a}->{b}",
            "cached_query": first[0], "cached_response": first[1], "new_query": second[0],
            "heuristic_label": False, "heuristic_source": "heuristic:intent_mismatch",
            "notes": "no shared intent token",
        })


def make_add(pairs: list[dict[str, Any]], seen: set) -> tuple[Any, dict[str, int]]:
    counts: dict[str, int] = defaultdict(int)
    seen_local = seen

    def record(pair: dict[str, Any]) -> bool:
        if add(pairs, seen_local, pair):
            counts[pair["category"]] += 1
            return True
        return False

    return record, counts


def build_qa(rows: list[tuple[str, str]], rng: random.Random, pairs: list[dict[str, Any]], seen: set) -> None:
    record, counts = make_add(pairs, seen)

    by_answer: dict[str, list[int]] = defaultdict(list)
    for index, (_, answer) in enumerate(rows):
        by_answer[norm(answer)].append(index)

    for indices in by_answer.values():
        if len(indices) < 2 or counts["same_answer"] >= QA_COUNTS["same_answer"]:
            continue
        if len(indices) > 40:
            sampled = [tuple(rng.sample(indices, 2)) for _ in range(200)]
        else:
            sampled = list(itertools.combinations(indices, 2))
        for i, j in sampled:
            if counts["same_answer"] >= QA_COUNTS["same_answer"]:
                break
            qi, ai = rows[i]
            qj = rows[j][0]
            if jaccard(qi, qj) < 0.3:
                continue
            record({
                "category": "same_answer", "domain": "open_domain_qa",
                "source": f"hf:{TRIVIA_ID}",
                "cached_query": qi, "cached_response": ai, "new_query": qj,
                "heuristic_label": True, "heuristic_source": "heuristic:similar_question+equal_answer",
                "notes": "verify the two questions are true paraphrases",
            })

    tokens = [set(norm(question).split()) for question, _ in rows]
    raw_answers = [answer for _, answer in rows]
    answers = [norm(answer) for answer in raw_answers]
    digits = [any(ch.isdigit() for ch in answer) for answer in raw_answers]
    n = len(rows)
    for i in range(n):
        if counts["entity_swap"] >= QA_COUNTS["entity_swap"] and counts["number_change"] >= QA_COUNTS["number_change"]:
            break
        ti, ai, di = tokens[i], answers[i], digits[i]
        li = len(ti)
        for j in range(i + 1, n):
            if ai == answers[j]:
                continue
            dj = digits[j]
            if di and dj:
                category = "number_change"
            elif not di and not dj:
                category = "entity_swap"
            else:
                continue
            if counts[category] >= QA_COUNTS[category]:
                continue
            tj = tokens[j]
            intersection = len(ti & tj)
            if intersection == 0:
                continue
            if intersection / (li + len(tj) - intersection) < 0.45:
                continue
            qi, ai_raw = rows[i]
            qj, aj_raw = rows[j]
            record({
                "category": category, "domain": "open_domain_qa",
                "source": f"hf:{TRIVIA_ID}",
                "cached_query": qi, "cached_response": ai_raw, "new_query": qj,
                "heuristic_label": False, "heuristic_source": "heuristic:similar_question+different_answer",
                "notes": f"answers differ: '{ai_raw}' vs '{aj_raw}'",
            })


def cluster_key(pair: dict[str, Any]) -> str:
    answer = " ".join(sorted(WORD.findall((pair.get("cached_response") or "").lower())))
    return f"{pair.get('source', '')}::{answer}"


def split_for(pair: dict[str, Any], test_modulus: int = 6) -> str:
    digest = hashlib.md5(cluster_key(pair).encode()).hexdigest()
    return "test" if int(digest, 16) % test_modulus == 0 else "dev"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=Path("pool.jsonl"))
    parser.add_argument("--seed", type=int, default=13)
    parser.add_argument("--qa-questions", type=int, default=4000)
    parser.add_argument("--test-modulus", type=int, default=6)
    args = parser.parse_args()
    rng = random.Random(args.seed)

    pairs: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    try:
        build_support(collect_support(), rng, pairs, seen)
    except Exception as exc:  # noqa: BLE001
        print(f"warning: support build failed: {exc}")
    try:
        build_qa(collect_qa(args.qa_questions), rng, pairs, seen)
    except Exception as exc:  # noqa: BLE001
        print(f"warning: QA build failed: {exc}")

    for index, pair in enumerate(pairs):
        pair["id"] = f"pool_{index:05d}"
        pair["split"] = split_for(pair, args.test_modulus)

    rng.shuffle(pairs)
    with args.out.open("w") as handle:
        for pair in pairs:
            handle.write(json.dumps(pair) + "\n")

    by_split: dict[str, int] = {}
    by_cell: dict[tuple[str, str], int] = defaultdict(int)
    for pair in pairs:
        by_split[pair["split"]] = by_split.get(pair["split"], 0) + 1
        by_cell[(pair["category"], pair["split"])] += 1
    print(f"wrote {len(pairs)} pairs to {args.out}")
    print(f"  splits: {by_split}")
    print(f"  {'category':<18} {'dev':>5} {'test':>5}")
    for category in sorted({c for c, _ in by_cell}):
        print(f"  {category:<18} {by_cell[(category, 'dev')]:>5} {by_cell[(category, 'test')]:>5}")
    return 0 if pairs else 1


if __name__ == "__main__":
    raise SystemExit(main())
