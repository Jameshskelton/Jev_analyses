#!/usr/bin/env python3
"""Embedding + cosine-similarity baselines for the semantic-cache experiment.

Implements the classical baseline from the plan: score each (new query, cached
entry) pair by cosine similarity of embeddings. Supports two comparisons:

    query_to_query     cosine(new_query, cached_query)
    query_to_response  cosine(new_query, cached_response)

Backends:
    tfidf   pure-python TF-IDF, no downloads, runs offline (weak lexical baseline)
    do      DigitalOcean serverless embeddings (GTE Large v1.5, BGE-M3, ...),
            same model access key as Jev; OpenAI-compatible /v1/embeddings
    openai  OpenAI embeddings endpoint via requests (needs OPENAI_API_KEY)
    st      sentence-transformers local model (e.g. BGE, GTE)

DigitalOcean embedding model IDs: gte-large-en-v1.5, bge-m3, e5-large-v2,
qwen3-embedding-0.6b, all-minilm-l6-v2, multi-qa-mpnet-base-dot-v1.
"""

from __future__ import annotations

import math
import os
import re
import time
from dataclasses import dataclass
from typing import Any

import requests

import costs

TOKEN = re.compile(r"[a-z0-9]+")


@dataclass
class ScoreRun:
    scores: list[float]
    batch_latency_ms: float
    input_tokens: int | None
    estimated_tokens: bool

    def latency_ms(self, count: int) -> float:
        return self.batch_latency_ms / count if count else self.batch_latency_ms


def tokenize(text: str) -> list[str]:
    return TOKEN.findall((text or "").lower())


def cosine(a: Any, b: Any) -> float:
    if isinstance(a, dict):
        if len(a) > len(b):
            a, b = b, a
        dot = sum(weight * b.get(term, 0.0) for term, weight in a.items())
        norm_a = math.sqrt(sum(weight * weight for weight in a.values()))
        norm_b = math.sqrt(sum(weight * weight for weight in b.values()))
    else:
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _env(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


class TfidfEmbedder:
    name = "tfidf"
    model_id = "tfidf"
    billable = False

    def __init__(self, corpus: list[str]):
        document_frequency: dict[str, int] = {}
        for text in corpus:
            for term in set(tokenize(text)):
                document_frequency[term] = document_frequency.get(term, 0) + 1
        total = len(corpus) or 1
        self.idf = {term: math.log((1 + total) / (1 + count)) + 1.0 for term, count in document_frequency.items()}

    def encode(self, texts: list[str]) -> list[dict[str, float]]:
        vectors: list[dict[str, float]] = []
        for text in texts:
            tokens = tokenize(text)
            length = len(tokens) or 1
            counts: dict[str, int] = {}
            for token in tokens:
                counts[token] = counts.get(token, 0) + 1
            vectors.append({term: (count / length) * self.idf.get(term, 1.0) for term, count in counts.items()})
        return vectors


class APIEmbedder:
    billable = True

    def __init__(self, model: str, api_key: str | None, base_url: str, name_prefix: str):
        self.model_id = model
        self.name = f"{name_prefix}_{model}"
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.last_input_tokens: int | None = None
        self.last_usage_seen = False

    def encode(self, texts: list[str], batch: int = 256) -> list[list[float]]:
        if not self.api_key:
            raise RuntimeError(f"no API key set for embedding backend '{self.name}'")
        vectors: list[list[float]] = []
        self.last_input_tokens = 0
        self.last_usage_seen = True
        for start in range(0, len(texts), batch):
            chunk = [text if text.strip() else " " for text in texts[start : start + batch]]
            response = requests.post(
                f"{self.base_url}/embeddings",
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json={"model": self.model_id, "input": chunk, "encoding_format": "float"},
                timeout=120.0,
            )
            response.raise_for_status()
            payload = response.json()
            data = payload["data"]
            vectors.extend([item["embedding"] for item in sorted(data, key=lambda item: item["index"])])
            usage = payload.get("usage") or {}
            tokens = usage.get("prompt_tokens") or usage.get("total_tokens")
            if tokens is None:
                self.last_usage_seen = False
                tokens = costs.estimate_tokens(chunk)
            self.last_input_tokens = (self.last_input_tokens or 0) + tokens
        return vectors


class SentenceTransformerEmbedder:
    billable = False

    def __init__(self, model: str):
        from sentence_transformers import SentenceTransformer

        self.model_id = model
        self.name = f"st_{re.sub(r'[^a-z0-9]+', '-', model.lower()).strip('-')}"
        self.model = SentenceTransformer(model)
        self.last_input_tokens = None

    def encode(self, texts: list[str]) -> list[list[float]]:
        vectors = self.model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
        return [list(vector) for vector in vectors]


def available_backends() -> list[str]:
    return ["tfidf", "do", "openai", "st"]


def default_backends() -> list[str]:
    return ["tfidf", "do"]


def build_embedder(backend: str, corpus: list[str], options: dict[str, Any]) -> list[Any]:
    if backend == "tfidf":
        return [TfidfEmbedder(corpus)]
    if backend == "do":
        return [APIEmbedder(model, options["do_key"], options["do_base"], "do") for model in options["do_models"]]
    if backend == "openai":
        return [
            APIEmbedder(model, options["openai_key"], options["openai_base"], "openai")
            for model in options["openai_models"]
        ]
    if backend == "st":
        return [SentenceTransformerEmbedder(model) for model in options["st_models"]]
    raise ValueError(f"unknown baseline backend: {backend}")


def default_options() -> dict[str, Any]:
    return {
        "do_key": _env("MODEL_ACCESS_KEY", "DIGITALOCEAN_TOKEN", "DO_API_TOKEN"),
        "do_base": _env("DO_EMBEDDINGS_BASE") or "https://inference.do-ai.run/v1",
        "do_models": [m.strip() for m in (_env("DO_EMBED_MODELS") or "gte-large-en-v1.5").split(",") if m.strip()],
        "openai_key": _env("OPENAI_API_KEY"),
        "openai_base": _env("OPENAI_BASE_URL") or "https://api.openai.com/v1",
        "openai_models": [m.strip() for m in (_env("OPENAI_EMBED_MODELS") or "text-embedding-3-small").split(",") if m.strip()],
        "st_models": [m.strip() for m in (_env("ST_EMBED_MODELS") or "BAAI/bge-small-en-v1.5,thenlper/gte-small").split(",") if m.strip()],
    }


def score_pairs(pairs: list[dict[str, Any]], embedder: Any, compare: str) -> ScoreRun:
    cached_key = "cached_query" if compare == "query_to_query" else "cached_response"
    texts = [pair["new_query"] for pair in pairs] + [pair[cached_key] for pair in pairs]
    start = time.monotonic()
    vectors = embedder.encode(texts)
    batch_latency_ms = (time.monotonic() - start) * 1000.0
    count = len(pairs)
    scores = [cosine(vectors[i], vectors[count + i]) for i in range(count)]
    estimated = not getattr(embedder, "last_usage_seen", False)
    return ScoreRun(
        scores=scores,
        batch_latency_ms=batch_latency_ms,
        input_tokens=getattr(embedder, "last_input_tokens", None),
        estimated_tokens=estimated,
    )
