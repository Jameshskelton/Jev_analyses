#!/usr/bin/env python3
"""General-purpose LLM baseline: ask a chat model the same cache question.

This is the "why not just prompt an LLM?" baseline. It asks a model (not Jev,
and ideally not the label judge) whether the cached response is a correct answer
to the new query, and records the probability it gives. The output uses the same
per-pair schema as run_pilot.py, so analyze.py can score it alongside Jev and
the cosine baselines.

Use a model that is neither the judge (deepseek-v4-pro) nor the assistant
(deepseek-v4.1-flash) to avoid circularity; default is qwen3.5-397b-a17b.

Usage:
    export MODEL_ACCESS_KEY=...
    python run_llm_baseline.py --pairs sample.jsonl --models qwen3.5-397b-a17b \
        --out llm_results.jsonl --stamp
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

import costs
import run_judge
import run_pilot

DEFAULT_URL = "https://inference.do-ai.run/v1/chat/completions"
DEFAULT_MODELS = "qwen3.5-397b-a17b"

SYSTEM = (
    "You decide whether a semantic cache may serve a stored response. Given a cached_query, the "
    "cached_response that was stored, and a new_query, judge whether serving the cached response is "
    "a correct answer to the new query. Answer with strict JSON and nothing else: "
    '{"serve": true|false, "probability": 0.0-1.0, "reason": "short reason"}. '
    "probability is the probability that serving the cached response is correct."
)


def sanitize(model: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-." else "-" for ch in model.lower())


def to_probability(parsed: dict[str, Any]) -> float | None:
    value = parsed.get("probability")
    if isinstance(value, (int, float)):
        return min(1.0, max(0.0, float(value)))
    serve = parsed.get("serve")
    if isinstance(serve, bool):
        return 1.0 if serve else 0.0
    if isinstance(serve, str):
        return 1.0 if serve.strip().lower() in {"true", "yes", "1"} else 0.0
    return None


def run_one(session: requests.Session, pair: dict[str, Any], model: str, args: argparse.Namespace) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "temperature": args.temperature,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": json.dumps({
                "cached_query": pair["cached_query"],
                "cached_response": pair["cached_response"],
                "new_query": pair["new_query"],
            })},
        ],
    }
    result = {
        "id": pair["id"],
        "state_mode": f"llm_{sanitize(model)}",
        "category": pair.get("category", "uncategorized"),
        "domain": pair.get("domain", "unknown"),
        "label_source": "llm-baseline",
        "cached_query": pair["cached_query"],
        "cached_response": pair["cached_response"],
        "new_query": pair["new_query"],
        "should_hit": bool(pair.get("should_hit", pair.get("heuristic_label", False))),
        "system": f"llm:{model}",
        "score": None,
        "predicted_hit": None,
        "correct": None,
        "latency_ms": None,
        "input_tokens": None,
        "output_tokens": None,
        "cost_usd": None,
        "model": model,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "attempts": 0,
        "error": None,
    }
    if args.json_mode:
        payload["response_format"] = {"type": "json_object"}
    try:
        try:
            data, attempts, latency_ms = run_pilot.post_with_retries(
                session, args.url, args.headers, payload, args.timeout, args.max_retries
            )
        except requests.HTTPError as exc:
            status = getattr(exc.response, "status_code", None)
            if args.json_mode and status in {400, 422}:
                payload.pop("response_format", None)
                data, attempts, latency_ms = run_pilot.post_with_retries(
                    session, args.url, args.headers, payload, args.timeout, args.max_retries
                )
            else:
                raise
        content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
        parsed = run_judge.extract_json(content) or {}
        result["score"] = to_probability(parsed)
        result["raw_reason"] = parsed.get("reason")
        usage = data.get("usage") or {}
        result["input_tokens"] = usage.get("prompt_tokens")
        result["output_tokens"] = usage.get("completion_tokens")
        result["cost_usd"] = costs.chat_cost(model, result["input_tokens"], result["output_tokens"])
        result["latency_ms"] = latency_ms
        result["attempts"] = attempts
        if result["score"] is not None:
            result["predicted_hit"] = result["score"] >= args.threshold
            result["correct"] = result["predicted_hit"] == result["should_hit"]
        else:
            result["error"] = f"unparseable: {content[:120]!r}"
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pairs", type=Path, default=Path("sample.jsonl"))
    parser.add_argument("--out", type=Path, default=Path("llm_results.jsonl"))
    parser.add_argument("--url", default=os.environ.get("LLM_URL", DEFAULT_URL))
    parser.add_argument("--models", default=os.environ.get("LLM_MODELS", DEFAULT_MODELS), help="comma-separated")
    parser.add_argument("--threshold", type=float, default=0.5)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--max-retries", type=int, default=4)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--seed", type=int, default=13)
    parser.add_argument("--stamp", action="store_true")
    parser.add_argument("--no-json-mode", action="store_true")
    args = parser.parse_args()

    random.seed(args.seed)
    if args.stamp:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        args.out = args.out.with_name(f"{args.out.stem}_{stamp}{args.out.suffix}")

    api_key = os.environ.get("MODEL_ACCESS_KEY") or os.environ.get("DIGITALOCEAN_TOKEN")
    if not api_key:
        raise SystemExit("set MODEL_ACCESS_KEY or DIGITALOCEAN_TOKEN")
    args.headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    args.json_mode = not args.no_json_mode

    pairs = [json.loads(line) for line in args.pairs.read_text().splitlines() if line.strip()]
    if args.limit:
        pairs = pairs[: args.limit]
    if not pairs:
        raise SystemExit(f"no pairs in {args.pairs}")

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    print(f"models={models} pairs={len(pairs)} workers={args.workers}")
    session = requests.Session()
    tasks = [(pair, model) for model in models for pair in pairs]

    def worker(task: tuple[dict[str, Any], str]) -> dict[str, Any]:
        return run_one(session, task[0], task[1], args)

    records: list[dict[str, Any]] = []
    with args.out.open("w") as handle, ThreadPoolExecutor(max_workers=args.workers) as pool:
        for count, record in enumerate(pool.map(worker, tasks), 1):
            records.append(record)
            handle.write(json.dumps(record) + "\n")
            handle.flush()
            if count % 50 == 0 or count == len(tasks):
                print(f"  {count}/{len(tasks)} done")

    scored = [r for r in records if r["score"] is not None]
    total_cost = sum(r["cost_usd"] or 0.0 for r in records)
    print(f"\nscored {len(scored)}/{len(records)}  cost=${total_cost:.4f}  errors={len(records) - len(scored)}")
    print(f"wrote {args.out}")
    return 1 if len(scored) != len(records) else 0


if __name__ == "__main__":
    sys.exit(main())
