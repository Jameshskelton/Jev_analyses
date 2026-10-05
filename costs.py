#!/usr/bin/env python3
"""Cost model for the semantic-cache experiment.

Prices are DigitalOcean serverless inference rates, USD per 1M tokens, from
https://docs.digitalocean.com/products/inference/details/pricing/ (verified 2026-09-28).

    Jev                 input $0.042, output N/A (billed as 0)
    gte-large-en-v1.5   $0.09
    bge-m3              $0.02
    e5-large-v2         $0.02
    qwen3-embedding-0.6b $0.04
    all-minilm-l6-v2    $0.009
    multi-qa-mpnet-base-dot-v1 $0.009
"""

from __future__ import annotations

JEV_INPUT_PER_1M = 0.042
JEV_OUTPUT_PER_1M = 0.0

CHAT_PER_1M = {
    "deepseek-v4.1-flash": (0.30, 1.20),
    "deepseek-v4-pro": (1.32, 3.96),
    "qwen3.5-397b-a17b": (0.55, 3.50),
    "glm-5.3": (1.40, 4.40),
    "anthropic-claude-haiku-4.5": (1.00, 5.00),
    "openai-gpt-4o-mini": (0.15, 0.60),
    "openai-gpt-5-nano": (0.05, 0.40),
    "openai-gpt-5-mini": (0.25, 2.00),
    "ministral-3-14b-instruct-2512": (0.20, 0.20),
}
DEFAULT_CHAT_PER_1M = (0.30, 1.20)

EMBEDDING_PER_1M = {
    "gte-large-en-v1.5": 0.09,
    "bge-m3": 0.02,
    "e5-large-v2": 0.02,
    "multilingual-e5-large": 0.02,
    "qwen3-embedding-0.6b": 0.04,
    "all-minilm-l6-v2": 0.009,
    "all-mini-lm-l6-v2": 0.009,
    "multi-qa-mpnet-base-dot-v1": 0.009,
}
DEFAULT_EMBEDDING_PER_1M = 0.09
LOCAL_COST = 0.0


def jev_cost(input_tokens: int | None, output_tokens: int | None = 0) -> float:
    return ((input_tokens or 0) * JEV_INPUT_PER_1M + (output_tokens or 0) * JEV_OUTPUT_PER_1M) / 1_000_000


def chat_cost(model_id: str | None, input_tokens: int | None, output_tokens: int | None) -> float:
    in_price, out_price = CHAT_PER_1M.get(model_id or "", DEFAULT_CHAT_PER_1M)
    return ((input_tokens or 0) * in_price + (output_tokens or 0) * out_price) / 1_000_000


def embedding_cost(model_id: str | None, tokens: int | None) -> float:
    price = EMBEDDING_PER_1M.get(model_id or "", DEFAULT_EMBEDDING_PER_1M)
    return (tokens or 0) * price / 1_000_000


def estimate_tokens(texts: list[str]) -> int:
    return max(1, sum(len(text) for text in texts) // 4)
