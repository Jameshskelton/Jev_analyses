#!/usr/bin/env python3
"""Headline metrics with bootstrap confidence intervals for the cache experiment.

Merges one or more run-result JSONL files (Jev + cosine baselines + the general
LLM baseline), scores them against a frozen gold-label file, and reports AUC,
precision and hit rates with 95% bootstrap CIs. The false-hit-budget hit rate is
computed by re-selecting the threshold inside each bootstrap replicate, so the
interval reflects threshold selection, not just sampling noise.

Usage:
    python analyze.py --gold gold_adjudicated.jsonl \
        --results results_adjudicated_<ts>.jsonl,llm_results_<ts>.jsonl \
        --domain ALL --n-boot 2000
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path
from typing import Callable

NICE = {
    "json_query_and_response": "Jev (q+resp)",
    "json_response_only": "Jev (resp only)",
    "plain_string": "Jev (plain)",
    "cosine_do_gte-large-en-v1.5_query_to_query": "GTE-L v1.5 q->q",
    "cosine_do_gte-large-en-v1.5_query_to_response": "GTE-L v1.5 q->resp",
    "cosine_tfidf_query_to_query": "tfidf q->q",
    "cosine_tfidf_query_to_response": "tfidf q->resp",
}


def load_gold(path: Path) -> dict[str, bool]:
    gold = {}
    for line in path.read_text().splitlines():
        if line.strip():
            row = json.loads(line)
            if row.get("should_hit") is not None:
                gold[str(row["id"])] = bool(row["should_hit"])
    return gold


def load_results(paths: list[Path], gold: dict[str, bool]) -> dict[str, list[dict]]:
    variants: dict[str, list[dict]] = {}
    for path in paths:
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("score") is None or str(row.get("id")) not in gold:
                continue
            variants.setdefault(row["state_mode"], []).append({
                "score": float(row["score"]),
                "label": gold[str(row["id"])],
                "domain": row.get("domain"),
                "latency_ms": row.get("latency_ms"),
                "cost_usd": row.get("cost_usd"),
            })
    return variants


def ops(records: list[dict]) -> tuple[float | None, float | None]:
    latencies = sorted(r["latency_ms"] for r in records if r.get("latency_ms") is not None)
    p50 = latencies[len(latencies) // 2] if latencies else None
    total_cost = sum(r.get("cost_usd") or 0.0 for r in records)
    per_1k = (total_cost / len(records) * 1000) if records else None
    return p50, per_1k


def auc(records: list[dict]) -> float | None:
    pos = [r["score"] for r in records if r["label"]]
    neg = [r["score"] for r in records if not r["label"]]
    if not pos or not neg:
        return None
    import run_pilot
    return run_pilot.auc(pos, neg)


def hit_rate(records: list[dict], threshold: float) -> float | None:
    pos = [r for r in records if r["label"]]
    if not pos:
        return None
    return sum(1 for r in pos if r["score"] >= threshold) / len(pos)


def precision(records: list[dict], threshold: float) -> float | None:
    served = [r for r in records if r["score"] >= threshold]
    if not served:
        return None
    return sum(1 for r in served if r["label"]) / len(served)


def false_hit_b(records: list[dict], threshold: float) -> float | None:
    served = [r for r in records if r["score"] >= threshold]
    if not served:
        return None
    return sum(1 for r in served if not r["label"]) / len(served)


def hit_at_budget(records: list[dict], budget: float, kind: str) -> float | None:
    pos = [r for r in records if r["label"]]
    neg = [r for r in records if not r["label"]]
    if not pos or not neg:
        return None
    best = None
    for threshold in sorted({r["score"] for r in records}, reverse=True):
        served = [r for r in records if r["score"] >= threshold]
        if not served:
            continue
        wrong = sum(1 for r in served if not r["label"])
        fha = wrong / len(neg)
        fhb = wrong / len(served)
        rate = fha if kind == "a" else fhb
        if rate > budget:
            continue
        hit = (len(pos) - sum(1 for r in pos if r["score"] < threshold)) / len(pos)
        if best is None or hit > best:
            best = hit
    return best


def bootstrap(records: list[dict], metric: Callable[[list[dict]], float | None], n: int, seed: int) -> tuple[float | None, float | None, float | None]:
    point = metric(records)
    rng = random.Random(seed)
    values = []
    size = len(records)
    for _ in range(n):
        sample = [records[rng.randrange(size)] for _ in range(size)]
        value = metric(sample)
        if value is not None:
            values.append(value)
    if not values:
        return point, None, None
    values.sort()
    lo = values[int(0.025 * len(values))]
    hi = values[min(len(values) - 1, int(0.975 * len(values)))]
    return point, lo, hi


def fmt(point, lo, hi, digits=2) -> str:
    if point is None:
        return "n/a"
    if lo is None:
        return f"{point:.{digits}f}"
    return f"{point:.{digits}f} [{lo:.{digits}f},{hi:.{digits}f}]"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--gold", type=Path, default=Path("gold_adjudicated.jsonl"))
    parser.add_argument("--results", required=True, help="comma-separated result JSONL files")
    parser.add_argument("--domain", default="ALL", help="ALL | customer_support | open_domain_qa")
    parser.add_argument("--threshold", type=float, default=0.5)
    parser.add_argument("--n-boot", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=13)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    gold = load_gold(args.gold)
    paths = [Path(p) for p in args.results.split(",") if p.strip()]
    variants = load_results(paths, gold)

    print(f"gold={args.gold} ({sum(gold.values())} hit / {len(gold) - sum(gold.values())} miss)  domain={args.domain}  n_boot={args.n_boot}")
    header = (f"{'system':<20}{'n':>4}{'hits':>5}  {'AUC':<16}{'prec@'+format(args.threshold,'.2f'):<16}"
              f"{'hit@1%a':<15}{'hit@5%a':<15}{'hit@1%b':<15}{'ms p50':>8}{'$/1k':>10}")
    print(header)

    summary = {}
    rows = []
    for variant, records in variants.items():
        if args.domain != "ALL":
            records = [r for r in records if r["domain"] == args.domain]
        if not records:
            continue
        thr = args.threshold
        metrics = {
            "auc": bootstrap(records, auc, args.n_boot, args.seed),
            "precision": bootstrap(records, lambda r: precision(r, thr), args.n_boot, args.seed),
            "hit_at_1pct_a": bootstrap(records, lambda r: hit_at_budget(r, 0.01, "a"), args.n_boot, args.seed),
            "hit_at_5pct_a": bootstrap(records, lambda r: hit_at_budget(r, 0.05, "a"), args.n_boot, args.seed),
            "hit_at_1pct_b": bootstrap(records, lambda r: hit_at_budget(r, 0.01, "b"), args.n_boot, args.seed),
        }
        label = NICE.get(variant, f"LLM {variant[4:]}" if variant.startswith("llm_") else variant)
        p50, per_1k = ops(records)
        print(f"{label:<20}{len(records):>4}{sum(1 for r in records if r['label']):>5}  "
              f"{fmt(*metrics['auc']):<16}{fmt(*metrics['precision']):<16}"
              f"{fmt(*metrics['hit_at_1pct_a']):<15}{fmt(*metrics['hit_at_5pct_a']):<15}{fmt(*metrics['hit_at_1pct_b']):<15}"
              f"{(f'{p50:.0f}' if p50 is not None else 'n/a'):>8}{(f'{per_1k:.4f}' if per_1k is not None else 'n/a'):>10}")
        rows.append(label)
        summary[label] = {
            "n": len(records),
            "n_hits": sum(1 for r in records if r["label"]),
            "latency_ms_p50": p50,
            "cost_per_1k_lookups": per_1k,
            **{k: {"point": v[0], "lo": v[1], "hi": v[2]} for k, v in metrics.items()},
        }

    if args.out:
        args.out.write_text(json.dumps({"gold": str(args.gold), "domain": args.domain, "systems": summary}, indent=2))
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
