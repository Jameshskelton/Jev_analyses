#!/usr/bin/env python3
"""Run pilot semantic-cache pairs against Jev and a cosine-similarity baseline.

For every pair (cached query, cached response, new query, should_hit) we produce
one score per system:

  Jev        one Noul question -- "Does the cached response contain a complete
             and correct answer to the new query?" -- and read the returned
             probability. High = serve the cached response.
  cosine     cosine similarity of embeddings, either new-query vs cached-query
             or new-query vs cached-response (the classical baseline).

The report gives AUC and, per system, the best-F1 operating point and the hit
rate achievable at fixed false-hit budgets -- the plan's headline numbers.

Usage:
    export MODEL_ACCESS_KEY=...            # or TYPESAFE_API_KEY
    python run_pilot.py --pairs pilot_pairs.jsonl --state-mode all --baselines all

    # offline, no key, no downloads:
    python run_pilot.py --pairs pilot_pairs.jsonl --state-mode all --baselines tfidf

Environment:
    MODEL_ACCESS_KEY / TYPESAFE_API_KEY  Jev bearer token
    TYPESAFE_URL, TYPESAFE_MODEL         override endpoint / model
    OPENAI_API_KEY                       for --baselines openai
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import random
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

import baselines
import costs

DEFAULT_URL = "https://inference.do-ai.run/v1/systemone"
DEFAULT_MODEL = "typesafe-jev-1.13.0"
QUESTION_ID = "contains_answer"
RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 529}

TRUE_CRITERION = "The cached response fully and correctly answers the new query"
FALSE_CRITERION = (
    "The cached response does not answer the new query, answers a different "
    "question, or only partially answers it"
)

MODE_ALIASES = {
    "json_both": "json_query_and_response",
    "json_query_and_response": "json_query_and_response",
    "json_response_only": "json_response_only",
    "plain": "plain_string",
    "plain_string": "plain_string",
}
MODE_EXPANSIONS = {
    "both": ["json_query_and_response", "json_response_only"],
    "all": ["json_query_and_response", "json_response_only", "plain_string"],
}
COMPARE_CHOICES = {"query_to_query", "query_to_response"}


GOLD_POSITIVE = {"hit", "yes", "y", "true", "t", "1", "correct"}
GOLD_NEGATIVE = {"miss", "no", "n", "false", "f", "0", "incorrect"}


def parse_gold_label(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in GOLD_POSITIVE:
            return True
        if lowered in GOLD_NEGATIVE:
            return False
    return None


def load_labels(path: Path) -> dict[str, bool]:
    labels: dict[str, bool] = {}
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        value = parse_gold_label(row.get("should_hit"))
        if value is None:
            value = parse_gold_label(row.get("label"))
        if value is not None and row.get("id") is not None:
            labels[str(row["id"])] = value
    return labels


def resolve_modes(raw: str) -> list[str]:
    modes: list[str] = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        candidates = MODE_EXPANSIONS.get(token, [MODE_ALIASES.get(token, token)])
        for mode in candidates:
            if mode not in MODE_ALIASES.values():
                raise SystemExit(f"unknown state mode: {token}")
            if mode not in modes:
                modes.append(mode)
    return modes


def resolve_baselines(raw: str) -> list[str]:
    resolved: list[str] = []
    for token in (raw or "").split(","):
        token = token.strip()
        if not token:
            continue
        if token == "all":
            candidates = baselines.default_backends()
        elif token in baselines.available_backends():
            candidates = [token]
        else:
            raise SystemExit(f"unknown baseline backend: {token}")
        for candidate in candidates:
            if candidate not in resolved:
                resolved.append(candidate)
    return resolved


def resolve_compare(raw: str) -> list[str]:
    if raw == "both":
        return ["query_to_query", "query_to_response"]
    if raw in COMPARE_CHOICES:
        return [raw]
    raise SystemExit(f"unknown --baseline-compare value: {raw}")


def build_request(pair: dict[str, Any], state_mode: str, use_criteria: bool, model: str) -> dict[str, Any]:
    cached_query = pair["cached_query"]
    cached_response = pair["cached_response"]
    new_query = pair["new_query"]

    question: dict[str, Any] = {"type": "noul"}
    if state_mode == "json_query_and_response":
        state: Any = {"cached_query": cached_query, "cached_response": cached_response, "new_query": new_query}
        question["instructions"] = "Does `cached_response` contain a complete and correct answer to `new_query`?"
    elif state_mode == "json_response_only":
        state = {"cached_response": cached_response, "new_query": new_query}
        question["instructions"] = "Does `cached_response` contain a complete and correct answer to `new_query`?"
    elif state_mode == "plain_string":
        state = (
            f"CACHED QUERY:\n{cached_query}\n\n"
            f"CACHED RESPONSE:\n{cached_response}\n\n"
            f"NEW QUERY:\n{new_query}"
        )
        question["instructions"] = "Does the CACHED RESPONSE contain a complete and correct answer to the NEW QUERY?"
    else:
        raise ValueError(state_mode)

    if use_criteria:
        question["criteria"] = {"true": TRUE_CRITERION, "false": FALSE_CRITERION}
    return {"model": model, "state": state, "questions": {QUESTION_ID: question}}


@dataclass
class Result:
    id: str
    state_mode: str
    category: str
    domain: str
    label_source: str
    cached_query: str
    cached_response: str
    new_query: str
    should_hit: bool
    system: str = "jev"
    score: float | None = None
    predicted_hit: bool | None = None
    correct: bool | None = None
    latency_ms: float | None = None
    batch_latency_ms: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    model: str | None = None
    timestamp: str | None = None
    attempts: int = 0
    error: str | None = None
    raw_answer: dict[str, Any] | None = field(default=None)


def post_with_retries(
    session: requests.Session,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout: float,
    max_retries: int,
) -> tuple[dict[str, Any], int, float]:
    last_error: Exception | None = None
    for attempt in range(1, max_retries + 1):
        start = time.monotonic()
        try:
            response = session.post(url, headers=headers, json=payload, timeout=timeout)
        except requests.RequestException as exc:
            last_error = exc
            if attempt == max_retries:
                raise
            time.sleep(min(30.0, (1.6**attempt) + random.random()))
            continue
        latency_ms = (time.monotonic() - start) * 1000.0
        if response.status_code in RETRY_STATUS:
            last_error = RuntimeError(f"HTTP {response.status_code}: {response.text[:200]}")
            if attempt == max_retries:
                raise last_error
            retry_after = response.headers.get("Retry-After")
            delay = float(retry_after) if retry_after and retry_after.isdigit() else (1.6**attempt) + random.random()
            time.sleep(min(30.0, delay))
            continue
        response.raise_for_status()
        return response.json(), attempt, latency_ms
    raise last_error if last_error else RuntimeError("request failed")


def run_task(session: requests.Session, pair: dict[str, Any], state_mode: str, args: argparse.Namespace) -> Result:
    result = Result(
        id=pair["id"],
        state_mode=state_mode,
        category=pair.get("category", "uncategorized"),
        domain=pair.get("domain", "unknown"),
        label_source=pair.get("label_source", "unknown"),
        cached_query=pair["cached_query"],
        cached_response=pair["cached_response"],
        new_query=pair["new_query"],
        should_hit=bool(pair.get("should_hit", pair.get("heuristic_label", False))),
        system="jev",
    )
    payload = build_request(pair, state_mode, args.use_criteria, args.model)
    result.timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    try:
        data, attempts, latency_ms = post_with_retries(session, args.url, args.headers, payload, args.timeout, args.max_retries)
        answer = (data.get("answers") or {}).get(QUESTION_ID) or {}
        score = answer.get("noul")
        result.score = float(score) if score is not None else None
        result.raw_answer = answer
        result.model = data.get("model")
        usage = data.get("usage") or {}
        result.input_tokens = usage.get("input_tokens")
        result.output_tokens = usage.get("output_tokens")
        result.cost_usd = costs.jev_cost(result.input_tokens, result.output_tokens)
        result.attempts = attempts
        result.latency_ms = latency_ms
        if result.score is not None:
            result.predicted_hit = result.score >= args.threshold
            result.correct = result.predicted_hit == result.should_hit
    except Exception as exc:  # noqa: BLE001
        result.error = f"{type(exc).__name__}: {exc}"
    return result


def build_baseline_results(pairs: list[dict[str, Any]], args: argparse.Namespace) -> tuple[list[Result], list[str]]:
    backends = resolve_baselines(args.baselines)
    if not backends:
        return [], []
    compares = resolve_compare(args.baseline_compare)
    corpus = (
        [pair["new_query"] for pair in pairs]
        + [pair["cached_query"] for pair in pairs]
        + [pair["cached_response"] for pair in pairs]
    )
    options = baselines.default_options()
    options["do_models"] = args.do_models
    options["openai_models"] = args.openai_models
    options["st_models"] = args.st_models
    if args.do_key:
        options["do_key"] = args.do_key
    results: list[Result] = []
    variants: list[str] = []
    for backend in backends:
        try:
            embedders = baselines.build_embedder(backend, corpus, options)
        except Exception as exc:  # noqa: BLE001
            print(f"  skipping baseline '{backend}': {exc}")
            continue
        for embedder in embedders:
            for compare in compares:
                variant = f"cosine_{embedder.name}_{compare}"
                try:
                    run = baselines.score_pairs(pairs, embedder, compare)
                except Exception as exc:  # noqa: BLE001
                    print(f"  skipping baseline '{variant}': {exc}")
                    continue
                variants.append(variant)
                count = len(pairs)
                batch_cost = costs.embedding_cost(embedder.model_id, run.input_tokens) if embedder.billable else costs.LOCAL_COST
                per_row_tokens = (run.input_tokens // count) if (run.input_tokens and count) else None
                per_row_cost = batch_cost / count if count else 0.0
                stamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
                for pair, score in zip(pairs, run.scores):
                    should_hit = bool(pair.get("should_hit", pair.get("heuristic_label", False)))
                    results.append(
                        Result(
                            id=pair["id"],
                            state_mode=variant,
                            category=pair.get("category", "uncategorized"),
                            domain=pair.get("domain", "unknown"),
                            label_source=pair.get("label_source", pair.get("heuristic_source", "unknown")),
                            cached_query=pair["cached_query"],
                            cached_response=pair["cached_response"],
                            new_query=pair["new_query"],
                            should_hit=should_hit,
                            system=f"cosine:{embedder.name}",
                            score=score,
                            predicted_hit=score >= args.threshold,
                            correct=(score >= args.threshold) == should_hit,
                            latency_ms=run.latency_ms(count),
                            batch_latency_ms=run.batch_latency_ms,
                            input_tokens=per_row_tokens,
                            cost_usd=per_row_cost,
                            model=embedder.model_id,
                            timestamp=stamp,
                            attempts=0,
                        )
                    )
    return results, variants


def auc(positive: list[float], negative: list[float]) -> float | None:
    if not positive or not negative:
        return None
    scores = positive + negative
    order = sorted(range(len(scores)), key=lambda i: scores[i])
    ranks = [0.0] * len(scores)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and scores[order[j + 1]] == scores[order[i]]:
            j += 1
        average_rank = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = average_rank
        i = j + 1
    rank_sum_positive = sum(ranks[i] for i in range(len(positive)))
    n_pos, n_neg = len(positive), len(negative)
    return (rank_sum_positive - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)


def _curve(results: list[Result]) -> list[dict[str, Any]]:
    scored = [r for r in results if r.score is not None]
    positive = [r for r in scored if r.should_hit]
    negative = [r for r in scored if not r.should_hit]
    curve = []
    for threshold in sorted({r.score for r in scored}, reverse=True):
        served = [r for r in scored if r.score >= threshold]
        true_positive = sum(1 for r in served if r.should_hit)
        wrong = len(served) - true_positive
        curve.append({
            "threshold": threshold,
            "hit_rate": (true_positive / len(positive)) if positive else None,
            "false_hit_a": (wrong / len(negative)) if negative else None,
            "false_hit_b": (wrong / len(served)) if served else 0.0,
        })
    return curve


def best_f1(results: list[Result]) -> tuple[float, float] | None:
    scored = [r for r in results if r.score is not None]
    positive = sum(1 for r in scored if r.should_hit)
    if not scored or positive == 0:
        return None
    best: tuple[float, float] | None = None
    for threshold in sorted({r.score for r in scored}, reverse=True):
        served = [r for r in scored if r.score >= threshold]
        true_positive = sum(1 for r in served if r.should_hit)
        precision = true_positive / len(served) if served else 0.0
        recall = true_positive / positive
        f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
        if best is None or f1 > best[1]:
            best = (threshold, f1)
    return best


def operating_points(results: list[Result], budgets: tuple[float, ...] = (0.01, 0.02, 0.05)) -> dict[str, Any]:
    curve = _curve(results)
    points: dict[str, Any] = {}
    for budget in budgets:
        best: dict[str, Any] | None = None
        for point in curve:
            if point["false_hit_b"] is None or point["false_hit_b"] > budget:
                continue
            if best is None or (point["hit_rate"] or 0.0) > (best["hit_rate"] or 0.0):
                best = point
        points[f"{budget:.0%}"] = best
    return points


def metrics(results: list[Result], threshold: float) -> dict[str, Any]:
    scored = [r for r in results if r.score is not None]
    positive = [r for r in scored if r.should_hit]
    negative = [r for r in scored if not r.should_hit]
    served = [r for r in scored if r.score >= threshold]
    wrong_served = [r for r in served if not r.should_hit]
    missed = [r for r in positive if r.score < threshold]
    f1_point = best_f1(results)
    return {
        "n": len(results),
        "n_scored": len(scored),
        "n_errors": len(results) - len(scored),
        "n_should_hit": len(positive),
        "n_should_miss": len(negative),
        "auc": auc([r.score for r in positive], [r.score for r in negative]),
        "mean_score_should_hit": (sum(r.score for r in positive) / len(positive)) if positive else None,
        "mean_score_should_miss": (sum(r.score for r in negative) / len(negative)) if negative else None,
        "hit_rate": (len(positive) - len(missed)) / len(positive) if positive else None,
        "false_hit_rate_a": (len(wrong_served) / len(negative)) if negative else None,
        "false_hit_rate_b": (len(wrong_served) / len(served)) if served else None,
        "precision": (1 - len(wrong_served) / len(served)) if served else None,
        "accuracy": (sum(1 for r in scored if r.correct) / len(scored)) if scored else None,
        "best_f1_threshold": f1_point[0] if f1_point else None,
        "best_f1": f1_point[1] if f1_point else None,
        "hit_rate_at_false_hit_budget": operating_points(results),
    }


def variant_stats(results: list[Result]) -> dict[str, Any]:
    latencies = sorted(r.latency_ms for r in results if r.latency_ms is not None)
    p50 = latencies[len(latencies) // 2] if latencies else None
    p95 = latencies[int(len(latencies) * 0.95) - 1] if latencies else None
    total_cost = sum(r.cost_usd or 0.0 for r in results)
    return {
        "latency_ms_p50": p50,
        "latency_ms_p95": p95,
        "cost_usd_total": total_cost,
        "cost_per_1k_lookups": (total_cost / len(results) * 1000) if results else None,
    }


def fmt(value: float | None, digits: int = 3) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def print_report(results: list[Result], variants: list[str], threshold: float) -> None:
    for variant in variants:
        subset = [r for r in results if r.state_mode == variant]
        if not subset:
            continue
        system = subset[0].system
        overall = metrics(subset, threshold)
        stats = variant_stats(subset)
        print(f"\n=== {variant}  [{system}]  n={len(subset)} ===")
        print(
            f"  latency_ms p50={fmt(stats['latency_ms_p50'], 0)} p95={fmt(stats['latency_ms_p95'], 0)}  "
            f"cost/1k=${fmt(stats['cost_per_1k_lookups'], 4)}"
        )
        print(
            f"  auc={fmt(overall['auc'])}  "
            f"mean(score|hit)={fmt(overall['mean_score_should_hit'])}  "
            f"mean(score|miss)={fmt(overall['mean_score_should_miss'])}  "
            f"best_f1={fmt(overall['best_f1'])}@thr={fmt(overall['best_f1_threshold'])}"
        )
        print(
            f"  @thr={threshold:.2f}  hit_rate={fmt(overall['hit_rate'])}  "
            f"false_hit(b)={fmt(overall['false_hit_rate_b'])}  "
            f"false_hit(a)={fmt(overall['false_hit_rate_a'])}  accuracy={fmt(overall['accuracy'])}"
        )
        for budget, point in overall["hit_rate_at_false_hit_budget"].items():
            if point:
                print(
                    f"  at false_hit(b)<={budget}: hit_rate={fmt(point['hit_rate'], 2)} "
                    f"@thr={fmt(point['threshold'], 2)}"
                )
            else:
                print(f"  at false_hit(b)<={budget}: unreachable")
        print(f"  {'category':<20} {'n':>4} {'hit%':>6} {'fhit(b)':>8} {'auc':>6}  domain")
        for category in sorted({r.category for r in subset}):
            rows = [r for r in subset if r.category == category]
            m = metrics(rows, threshold)
            domain = rows[0].domain if rows else ""
            print(
                f"  {category:<20} {m['n']:>4} {fmt(m['hit_rate'], 2):>6} "
                f"{fmt(m['false_hit_rate_b'], 2):>8} {fmt(m['auc'], 2):>6}  {domain}"
            )
        errors = [r for r in subset if r.error]
        if errors:
            print(f"  errors: {len(errors)} (first: {errors[0].error})")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pairs", type=Path, default=Path("pilot_pairs.jsonl"))
    parser.add_argument("--out", type=Path, default=Path("pilot_results.jsonl"))
    parser.add_argument("--summary", type=Path, default=Path("pilot_summary.json"))
    parser.add_argument("--url", default=os.environ.get("TYPESAFE_URL", DEFAULT_URL))
    parser.add_argument("--model", default=os.environ.get("TYPESAFE_MODEL", DEFAULT_MODEL))
    parser.add_argument("--state-mode", default="all", help="json_query_and_response | json_response_only | plain_string | both | all")
    parser.add_argument("--baselines", default="tfidf,do", help="tfidf | do | openai | st | all (comma-separated; default tfidf,do)")
    parser.add_argument("--baseline-compare", default="both", help="query_to_query | query_to_response | both")
    parser.add_argument("--do-embed-model", default="gte-large-en-v1.5", help="DigitalOcean embedding model(s), comma-separated, e.g. gte-large-en-v1.5,bge-m3,e5-large-v2")
    parser.add_argument("--do-key", default=os.environ.get("MODEL_ACCESS_KEY") or os.environ.get("DIGITALOCEAN_TOKEN"))
    parser.add_argument("--openai-embed-model", default="text-embedding-3-small")
    parser.add_argument("--st-model", default="BAAI/bge-small-en-v1.5,thenlper/gte-small")
    parser.add_argument("--threshold", type=float, default=0.5)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--max-retries", type=int, default=4)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--no-criteria", action="store_true")
    parser.add_argument("--baseline-only", action="store_true", help="skip Jev, run only the cosine baselines")
    parser.add_argument("--labels", type=Path, default=None, help="JSONL gold labels {id, should_hit|label} to override heuristic labels")
    parser.add_argument("--seed", type=int, default=13, help="seed for retry jitter (reproducibility)")
    parser.add_argument("--stamp", action="store_true", help="append a UTC timestamp to --out/--summary so runs never overwrite")
    parser.add_argument("--list-models", action="store_true", help="GET the DigitalOcean model list and exit")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.stamp:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        args.out = args.out.with_name(f"{args.out.stem}_{stamp}{args.out.suffix}")
        args.summary = args.summary.with_name(f"{args.summary.stem}_{stamp}{args.summary.suffix}")

    args.use_criteria = not args.no_criteria
    args.st_models = [model.strip() for model in args.st_model.split(",") if model.strip()]
    args.do_models = [model.strip() for model in args.do_embed_model.split(",") if model.strip()]
    args.openai_models = [model.strip() for model in args.openai_embed_model.split(",") if model.strip()]
    modes = resolve_modes(args.state_mode)
    random.seed(args.seed)

    if args.list_models:
        options = baselines.default_options()
        if args.do_key:
            options["do_key"] = args.do_key
        if not options.get("do_key"):
            raise SystemExit("set MODEL_ACCESS_KEY or DIGITALOCEAN_TOKEN for --list-models")
        response = requests.get(
            f"{options['do_base']}/models",
            headers={"Authorization": f"Bearer {options['do_key']}"},
            timeout=60.0,
        )
        response.raise_for_status()
        payload = response.json()
        items = payload.get("data") or payload.get("models") or payload
        ids = sorted({item.get("id") for item in items if isinstance(item, dict) and item.get("id")})
        print("\n".join(ids))
        return 0

    pairs = [json.loads(line) for line in args.pairs.read_text().splitlines() if line.strip()]
    if args.limit:
        pairs = pairs[: args.limit]
    if not pairs:
        raise SystemExit(f"no pairs found in {args.pairs}")

    if args.dry_run:
        for mode in modes:
            print(f"--- {mode} ---")
            print(json.dumps(build_request(pairs[0], mode, args.use_criteria, args.model), indent=2))
        backends = resolve_baselines(args.baselines)
        print(f"\npairs={len(pairs)}  jev_modes={len(modes)}  baselines={backends or 'none'}")
        return 0

    api_key = os.environ.get("MODEL_ACCESS_KEY") or os.environ.get("TYPESAFE_API_KEY")
    run_jev = bool(api_key) and not args.baseline_only
    if not run_jev and not args.baselines:
        raise SystemExit("set MODEL_ACCESS_KEY/TYPESAFE_API_KEY, or pass --baselines")

    args.headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"} if api_key else {}
    results: list[Result] = []
    variants: list[str] = []
    baseline_variants: list[str] = []
    baseline_rows: list[Result] = []

    if args.baselines:
        print(f"computing baselines: {resolve_baselines(args.baselines)} x {resolve_compare(args.baseline_compare)}")
        baseline_rows, baseline_variants = build_baseline_results(pairs, args)
        results.extend(baseline_rows)

    if run_jev:
        variants = modes
        print(f"jev: model={args.model} url={args.url} pairs={len(pairs)} modes={modes}")
        session = requests.Session()
        tasks = [(pair, mode) for pair in pairs for mode in modes]

        def worker(task: tuple[dict[str, Any], str]) -> Result:
            return run_task(session, task[0], task[1], args)

        with args.out.open("w") as handle, ThreadPoolExecutor(max_workers=args.workers) as pool:
            for result in baseline_rows:
                handle.write(json.dumps(asdict(result)) + "\n")
            for count, result in enumerate(pool.map(worker, tasks), 1):
                results.append(result)
                handle.write(json.dumps(asdict(result)) + "\n")
                if count % 25 == 0 or count == len(tasks):
                    print(f"  {count}/{len(tasks)} done")
                handle.flush()
    else:
        with args.out.open("w") as handle:
            for result in results:
                handle.write(json.dumps(asdict(result)) + "\n")
    variants = variants + baseline_variants

    if args.labels:
        gold = load_labels(args.labels)
        matched = 0
        for result in results:
            if result.id in gold:
                matched += 1
                result.should_hit = gold[result.id]
                if result.score is not None:
                    result.predicted_hit = result.score >= args.threshold
                    result.correct = result.predicted_hit == result.should_hit
        with args.out.open("w") as handle:
            for result in results:
                handle.write(json.dumps(asdict(result)) + "\n")
        print(f"applied gold labels to {matched}/{len(results)} rows from {args.labels}; rewrote {args.out}")

    scored = [r for r in results if r.score is not None]
    jev_scored = [r for r in scored if r.system == "jev"]
    latencies = sorted(r.latency_ms for r in jev_scored if r.latency_ms is not None)
    p50 = latencies[len(latencies) // 2] if latencies else None
    p95 = latencies[int(len(latencies) * 0.95) - 1] if latencies else None
    if jev_scored:
        print(f"\njev errors={len([r for r in results if r.system == 'jev']) - len(jev_scored)}")
    if latencies:
        print(f"jev latency_ms p50={p50:.0f} p95={p95:.0f}  avg input_tokens={sum(r.input_tokens or 0 for r in jev_scored) / len(jev_scored):.0f}")

    print_report(results, variants, args.threshold)

    summary = {
        "model": args.model,
        "url": args.url,
        "threshold": args.threshold,
        "seed": args.seed,
        "latency_ms": {"p50": p50, "p95": p95},
        "variants": {
            variant: {
                **metrics([r for r in results if r.state_mode == variant], args.threshold),
                **variant_stats([r for r in results if r.state_mode == variant]),
            }
            for variant in variants
        },
    }
    args.summary.write_text(json.dumps(summary, indent=2))
    print(f"\nwrote {args.out} and {args.summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
