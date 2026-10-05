#!/usr/bin/env python3
"""Independent LLM judge for cache-hit correctness (Phase 2).

Asks a non-Jev model (default deepseek-v4.1-flash on DigitalOcean chat
completions) the same question the semantic cache cares about: would serving the
cached response be a correct answer to the new query? Writes one label per pair.

This judge is validated against human labels (agreement.py) before it is trusted
for the bulk of the pool; ground truth never comes from Jev.

Usage:
    export MODEL_ACCESS_KEY=...
    python run_judge.py --pairs sample.jsonl --out judge_labels.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

import costs

DEFAULT_URL = "https://inference.do-ai.run/v1/chat/completions"
DEFAULT_MODEL = "deepseek-v4.1-flash"
RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 529}

SYSTEM = (
    "You evaluate a semantic cache. Given a cached_query, the cached_response that was stored, "
    "and a new_query, decide whether serving cached_response as the answer to new_query would be "
    "correct. Answer with strict JSON and nothing else: "
    '{"label":"hit"|"miss","confidence":0.0-1.0,"reason":"short reason"}. '
    "'hit' means the cached response fully and correctly answers the new query. "
    "'miss' means it does not (different question, wrong entity or number, negation, broader or "
    "narrower scope, partial answer, stale, or an unrelated/refusal response)."
)


def extract_json(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def to_label(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"hit", "yes", "true", "1", "correct", "answer"}:
            return True
        if lowered in {"miss", "no", "false", "0", "incorrect", "not_answer"}:
            return False
    return None


def post(session: requests.Session, payload: dict[str, Any], args: argparse.Namespace) -> tuple[dict[str, Any], int, float]:
    last_error: Exception | None = None
    for attempt in range(1, args.max_retries + 1):
        start = time.monotonic()
        try:
            response = session.post(args.url, headers=args.headers, json=payload, timeout=args.timeout)
        except requests.RequestException as exc:
            last_error = exc
            if attempt == args.max_retries:
                raise
            time.sleep(min(30.0, (1.6**attempt) + random.random()))
            continue
        latency_ms = (time.monotonic() - start) * 1000.0
        if response.status_code in RETRY_STATUS:
            last_error = RuntimeError(f"HTTP {response.status_code}: {response.text[:200]}")
            if attempt == args.max_retries:
                raise last_error
            retry_after = response.headers.get("Retry-After")
            delay = float(retry_after) if retry_after and retry_after.isdigit() else (1.6**attempt) + random.random()
            time.sleep(min(30.0, delay))
            continue
        response.raise_for_status()
        return response.json(), attempt, latency_ms
    raise last_error if last_error else RuntimeError("request failed")


def judge_one(session: requests.Session, pair: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": args.model,
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
    record: dict[str, Any] = {
        "id": pair["id"],
        "model": args.model,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "label": None,
        "should_hit": None,
        "confidence": None,
        "reason": None,
        "input_tokens": None,
        "output_tokens": None,
        "cost_usd": None,
        "latency_ms": None,
        "attempts": 0,
        "error": None,
    }
    use_json_mode = args.json_mode
    if use_json_mode:
        payload["response_format"] = {"type": "json_object"}
    try:
        try:
            data, attempts, latency_ms = post(session, payload, args)
        except requests.HTTPError as exc:
            status = getattr(exc.response, "status_code", None)
            if use_json_mode and status in {400, 422}:
                payload.pop("response_format", None)
                data, attempts, latency_ms = post(session, payload, args)
            else:
                raise
        content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
        parsed = extract_json(content) or {}
        label = to_label(parsed.get("label"))
        record["should_hit"] = label
        record["label"] = "hit" if label is True else "miss" if label is False else None
        record["confidence"] = parsed.get("confidence")
        record["reason"] = parsed.get("reason")
        usage = data.get("usage") or {}
        record["input_tokens"] = usage.get("prompt_tokens")
        record["output_tokens"] = usage.get("completion_tokens")
        record["cost_usd"] = costs.chat_cost(args.model, record["input_tokens"], record["output_tokens"])
        record["latency_ms"] = latency_ms
        record["attempts"] = attempts
        if label is None:
            record["error"] = f"unparseable label: {content[:120]!r}"
    except Exception as exc:  # noqa: BLE001
        record["error"] = f"{type(exc).__name__}: {exc}"
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pairs", type=Path, default=Path("sample.jsonl"))
    parser.add_argument("--out", type=Path, default=Path("judge_labels.jsonl"))
    parser.add_argument("--url", default=os.environ.get("JUDGE_URL", DEFAULT_URL))
    parser.add_argument("--model", default=os.environ.get("JUDGE_MODEL", DEFAULT_MODEL))
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--max-retries", type=int, default=4)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--seed", type=int, default=13)
    parser.add_argument("--stamp", action="store_true")
    parser.add_argument("--no-json-mode", action="store_true", help="do not send response_format=json_object")
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

    print(f"judge model={args.model} url={args.url} pairs={len(pairs)} workers={args.workers}")
    session = requests.Session()

    def worker(pair: dict[str, Any]) -> dict[str, Any]:
        return judge_one(session, pair, args)

    records: list[dict[str, Any]] = []
    with args.out.open("w") as handle, ThreadPoolExecutor(max_workers=args.workers) as pool:
        for count, record in enumerate(pool.map(worker, pairs), 1):
            records.append(record)
            handle.write(json.dumps(record) + "\n")
            handle.flush()
            if count % 50 == 0 or count == len(pairs):
                print(f"  {count}/{len(pairs)} done")

    labelled = [r for r in records if r["should_hit"] is not None]
    hits = sum(1 for r in labelled if r["should_hit"])
    total_cost = sum(r["cost_usd"] or 0.0 for r in records)
    latencies = sorted(r["latency_ms"] for r in records if r["latency_ms"] is not None)
    p50 = latencies[len(latencies) // 2] if latencies else 0.0
    print(f"\nlabelled {len(labelled)}/{len(records)}  (hit={hits}, miss={len(labelled) - hits})")
    print(f"latency_ms p50={p50:.0f}  cost=${total_cost:.4f}  errors={len(records) - len(labelled)}")
    print(f"wrote {args.out}")
    return 1 if len(labelled) != len(records) else 0


if __name__ == "__main__":
    sys.exit(main())
