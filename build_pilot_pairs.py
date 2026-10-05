#!/usr/bin/env python3
"""Build pilot_pairs.jsonl from public HuggingFace datasets.

No text is generated. Every pair is assembled from existing dataset records:

  customer support (Bitext):  (instruction, intent, response)
      - paraphrase        same intent, two different instructions -> should hit
      - near_miss_intent  two intents that share a token            -> should miss
      - unrelated         two intents with no shared token          -> should miss

  open-domain QA (TriviaQA rc.nocontext): (question, short answer)
      - same_answer       similar questions, identical answer       -> should hit
      - entity_swap       similar questions, different text answer  -> should miss
      - number_change     similar questions, different number       -> should miss

TriviaQA labels are heuristic (question similarity + answer equality), not
gold. They are adequate for a formatting pilot; the main experiment must replace
them with judge/human labels. Each row records its label_source for that reason.

Usage:
    python build_pilot_pairs.py --out pilot_pairs.jsonl --seed 13
"""

from __future__ import annotations

import argparse
import itertools
import json
import random
import re
from pathlib import Path
from typing import Any, Iterable

from datasets import load_dataset

BITEXT_ID = "bitext/Bitext-customer-support-llm-chatbot-training-dataset"
TRIVIA_ID = "mandarjoshi/trivia_qa"
PLACEHOLDER = re.compile(r"\{\{.*?\}\}")
WORD = re.compile(r"[a-z0-9]+")


def norm(text: str) -> str:
    return " ".join(WORD.findall((text or "").lower()))


def jaccard(a: str, b: str) -> float:
    sa, sb = set(norm(a).split()), set(norm(b).split())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def intent_tokens(intent: str) -> set[str]:
    return set(re.split(r"[_\s]+", (intent or "").lower())) - {""}


def stream_rows(dataset: str, config: str | None, limit: int) -> Iterable[dict[str, Any]]:
    args = (dataset, config) if config else (dataset,)
    ds = load_dataset(*args, split="train", streaming=True)
    for count, row in enumerate(ds):
        if count >= limit:
            break
        yield row


def collect_bitext(rng: random.Random, target: int) -> dict[str, list[tuple[str, str, str]]]:
    by_intent: dict[str, list[tuple[str, str, str]]] = {}
    seen: set[str] = set()
    for row in stream_rows(BITEXT_ID, None, 40000):
        instruction = (row.get("instruction") or "").strip()
        response = (row.get("response") or "").strip()
        intent = (row.get("intent") or "").strip()
        if not intent or not (4 <= len(instruction) <= 300) or not (3 <= len(response) <= 400):
            continue
        if PLACEHOLDER.search(response):
            continue
        key = instruction.lower()
        if key in seen:
            continue
        seen.add(key)
        bucket = by_intent.setdefault(intent, [])
        if len(bucket) < 5:
            bucket.append((instruction, response, (row.get("category") or "").strip()))
        if sum(len(v) for v in by_intent.values()) >= target * 8:
            break
    return by_intent


def build_bitext(rng: random.Random, counts: dict[str, int]) -> list[dict[str, Any]]:
    by_intent = collect_bitext(rng, sum(counts.values()))
    pairs: list[dict[str, Any]] = []

    multi = sorted(i for i, v in by_intent.items() if len(v) >= 2)
    rng.shuffle(multi)
    for intent in multi:
        if sum(1 for p in pairs if p["category"] == "paraphrase") >= counts["paraphrase"]:
            break
        first, second = by_intent[intent][0], by_intent[intent][1]
        pairs.append({
            "id": f"bitext_paraphrase_{len(pairs):04d}",
            "category": "paraphrase",
            "domain": "customer_support",
            "cached_query": first[0],
            "cached_response": first[1],
            "new_query": second[0],
            "should_hit": True,
            "label_source": "native:intent_match",
            "source": f"hf:{BITEXT_ID}:intent={intent}",
            "notes": "same intent, different phrasing",
        })

    intents = list(by_intent)
    near_pool: list[tuple[str, str]] = []
    for a, b in itertools.combinations(intents, 2):
        shared = intent_tokens(a) & intent_tokens(b)
        if shared and a != b:
            near_pool.append((a, b))
    rng.shuffle(near_pool)
    for a, b in near_pool:
        if sum(1 for p in pairs if p["category"] == "near_miss_intent") >= counts["near_miss_intent"]:
            break
        first, second = by_intent[a][0], by_intent[b][0]
        pairs.append({
            "id": f"bitext_near_miss_{len(pairs):04d}",
            "category": "near_miss_intent",
            "domain": "customer_support",
            "cached_query": first[0],
            "cached_response": first[1],
            "new_query": second[0],
            "should_hit": False,
            "label_source": "native:intent_mismatch",
            "source": f"hf:{BITEXT_ID}:intent={a}->{b}",
            "notes": f"intents share {sorted(intent_tokens(a) & intent_tokens(b))}",
        })

    far_pool = [(a, b) for a, b in itertools.combinations(intents, 2) if not (intent_tokens(a) & intent_tokens(b))]
    rng.shuffle(far_pool)
    for a, b in far_pool:
        if sum(1 for p in pairs if p["category"] == "unrelated") >= counts["unrelated"]:
            break
        first, second = by_intent[a][0], by_intent[b][0]
        pairs.append({
            "id": f"bitext_unrelated_{len(pairs):04d}",
            "category": "unrelated",
            "domain": "customer_support",
            "cached_query": first[0],
            "cached_response": first[1],
            "new_query": second[0],
            "should_hit": False,
            "label_source": "native:intent_mismatch",
            "source": f"hf:{BITEXT_ID}:intent={a}->{b}",
            "notes": "no shared intent token",
        })
    return pairs


def collect_trivia(rng: random.Random, limit: int) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    seen: set[str] = set()
    for row in stream_rows(TRIVIA_ID, "rc.nocontext", 200000):
        question = (row.get("question") or "").strip()
        answer = row.get("answer") or {}
        value = (answer.get("normalized_value") or answer.get("value") or "").strip() if isinstance(answer, dict) else str(answer).strip()
        if not question or not value:
            continue
        if len(norm(value).split()) > 4 or len(question) > 200:
            continue
        key = norm(question)
        if key in seen:
            continue
        seen.add(key)
        rows.append((question, value))
        if len(rows) >= limit:
            break
    return rows


def build_trivia(rng: random.Random, counts: dict[str, int], n_questions: int = 1200) -> list[dict[str, Any]]:
    rows = collect_trivia(rng, n_questions)
    pairs: list[dict[str, Any]] = []
    used: set[tuple[int, int]] = set()
    same_answer: dict[str, list[int]] = {}
    for index, (_, answer) in enumerate(rows):
        same_answer.setdefault(norm(answer), []).append(index)

    for indices in same_answer.values():
        if len(indices) < 2:
            continue
        if sum(1 for p in pairs if p["category"] == "same_answer") >= counts["same_answer"]:
            break
        for i, j in itertools.combinations(indices, 2):
            if jaccard(rows[i][0], rows[j][0]) >= 0.5:
                pair = (min(i, j), max(i, j))
                if pair in used:
                    continue
                used.add(pair)
                cached_q, cached_a = rows[i]
                pairs.append({
                    "id": f"trivia_same_answer_{len(pairs):04d}",
                    "category": "same_answer",
                    "domain": "open_domain_qa",
                    "cached_query": cached_q,
                    "cached_response": cached_a,
                    "new_query": rows[j][0],
                    "should_hit": True,
                    "label_source": "heuristic:similar_question+equal_answer",
                    "source": f"hf:{TRIVIA_ID}",
                    "notes": "verify the two questions are true paraphrases",
                })
                break
        if sum(1 for p in pairs if p["category"] == "same_answer") >= counts["same_answer"]:
            break

    def answer_has_digit(text: str) -> bool:
        return any(ch.isdigit() for ch in text)

    for i, j in itertools.combinations(range(len(rows)), 2):
        if sum(1 for p in pairs if p["category"] in {"entity_swap", "number_change"}) >= counts["entity_swap"] + counts["number_change"]:
            break
        qi, ai = rows[i]
        qj, aj = rows[j]
        if norm(ai) == norm(aj) or jaccard(qi, qj) < 0.5:
            continue
        pair = (min(i, j), max(i, j))
        if pair in used:
            continue
        numeric_i, numeric_j = answer_has_digit(ai), answer_has_digit(aj)
        if numeric_i and numeric_j:
            category = "number_change"
        elif not numeric_i and not numeric_j:
            category = "entity_swap"
        else:
            continue
        if sum(1 for p in pairs if p["category"] == category) >= counts[category]:
            continue
        used.add(pair)
        pairs.append({
            "id": f"trivia_{category}_{len(pairs):04d}",
            "category": category,
            "domain": "open_domain_qa",
            "cached_query": qi,
            "cached_response": ai,
            "new_query": qj,
            "should_hit": False,
            "label_source": "heuristic:similar_question+different_answer",
            "source": f"hf:{TRIVIA_ID}",
            "notes": f"answers differ: '{ai}' vs '{aj}'",
        })
    return pairs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=Path("pilot_pairs.jsonl"))
    parser.add_argument("--seed", type=int, default=13)
    args = parser.parse_args()
    rng = random.Random(args.seed)

    bitext_counts = {"paraphrase": 12, "near_miss_intent": 8, "unrelated": 8}
    trivia_counts = {"same_answer": 8, "entity_swap": 8, "number_change": 6}

    pairs: list[dict[str, Any]] = []
    try:
        pairs += build_bitext(rng, bitext_counts)
    except Exception as exc:  # noqa: BLE001
        print(f"warning: Bitext build failed: {exc}")
    try:
        pairs += build_trivia(rng, trivia_counts)
    except Exception as exc:  # noqa: BLE001
        print(f"warning: TriviaQA build failed: {exc}")

    rng.shuffle(pairs)
    with args.out.open("w") as handle:
        for pair in pairs:
            handle.write(json.dumps(pair) + "\n")

    by_category: dict[str, int] = {}
    for pair in pairs:
        by_category[pair["category"]] = by_category.get(pair["category"], 0) + 1
    print(f"wrote {len(pairs)} pairs to {args.out}")
    for category, count in sorted(by_category.items()):
        print(f"  {category:<18} {count}")
    return 0 if pairs else 1


if __name__ == "__main__":
    raise SystemExit(main())
