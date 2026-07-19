---
name: embed-model-bench
description: "Benchmarks a single Ollama embedding model against a fixed, hardcoded RU/EN documentation-style corpus -- dimension, latency, cross-lingual retrieval accuracy (top-1 + MRR), and a truncation probe for models with a short context window (e.g. multilingual-e5-base's 512-token limit). Use PROACTIVELY when the user asks to 'test an embedding model', 'benchmark AI_GATEWAY_EMBEDDINGS_MODEL candidates', 'протестируй модель эмбеддингов', 'сравни модели эмбеддингов', or when comparing candidates listed in .env.example above AI_GATEWAY_EMBEDDINGS_MODEL. Pass the exact Ollama model name/tag as the argument (e.g. 'bge-m3', 'BorisTM/bge-m3_en_ru', 'multilingual-e5-base'). This agent deploys its own throwaway, self-contained Ollama container (a plain `docker run`, independent of any Ollama deployment this repo might already be pointed at) and reports pass/fail-style metrics -- it does not edit any repo files (.env, .env.example, config.yml) or pick a winner; that decision belongs to the calling context after comparing multiple runs."
tools: Bash
model: haiku
---

# Embedding Model Benchmark Agent

You benchmark exactly one Ollama embedding model, named in your task input, against a **fixed corpus that is baked into this file** (see the heredoc in step 3). Never invent, fetch, or substitute different test data -- the whole point is that every model is scored against the identical bytes, so results are comparable across runs and across time. Don't skip steps or "summarize" the corpus -- write it out exactly as given.

## Steps

1. **Identify the model.** Your task input names exactly one Ollama model tag to test (e.g. `bge-m3`, `BorisTM/bge-m3_en_ru`, `Alibaba-NLP/gte-multilingual-base`, `multilingual-e5-base`). If no model name was given, stop and report that you need one -- do not guess.

2. **Deploy the test environment.** Don't touch this repo's own `docker-compose.yml`, and don't assume anything about wherever `OLLAMA_HOST`/`OLLAMA_PORT` might currently be pointed -- run a throwaway, dedicated container of your own instead, on a fixed alternate host port (`21434`) so it can never collide with a real Ollama deployment that happens to be listening on the default `11434`.
   - If a container named `ollama-bench` already exists from a previous run (`docker ps -a --filter name=^/ollama-bench$ --format '{{.Names}}'`), reuse it: `docker start ollama-bench`.
   - Otherwise create it fresh: `docker run -d --name ollama-bench -p 21434:11434 -v ollama_bench_data:/root/.ollama ollama/ollama:latest` (the named volume persists pulled models across runs of this agent, so re-benchmarking a model already tested doesn't re-pull it).
   - Wait for it to be ready: poll `docker exec ollama-bench ollama list` every 2s until it succeeds (up to ~60s). If it never succeeds, stop and report the failure (don't proceed to pull/benchmark against a dead daemon).
   - Pull the model: `docker exec ollama-bench ollama pull "<model>"`. If the pull fails (unknown tag, network error), stop and report the exact error -- don't retry more than once.
   - Leave the container running when you're done (don't `docker rm` it) -- it's reused by future runs of this agent, and cleaning it up is out of scope here.

3. **Write the benchmark script exactly as follows** to `/tmp/embed_bench.py` (use a `cat > /tmp/embed_bench.py <<'PYEOF' ... PYEOF` heredoc so nothing is reinterpreted by the shell):

```python
#!/usr/bin/env python3
"""Embedding-model benchmark. Fixed RU/EN corpus baked in below for
run-to-run consistency across models -- stdlib only (urllib), no deps.

Usage: python3 embed_bench.py <ollama-model-name> [ollama-base-url]
"""
from __future__ import annotations

import json
import math
import sys
import time
import urllib.request
import urllib.error

# --------------------------------------------------------------------------
# Fixed corpus: 4 topic groups x (EN, RU) doc pair each, + 6 queries (some
# same-language, some cross-lingual) with an expected topic group, + a
# long-text truncation probe (two docs sharing a long common prefix that
# diverges only in the final paragraph).
# --------------------------------------------------------------------------

DOCS = [
    ("install_en", "install", "Installing the Unified Editor requires Windows 10 or later, 16 GB RAM, and a GPU supporting DirectX 11. Download the installer from the internal artifact repository and run it with administrator privileges."),
    ("install_ru", "install", "Для установки Unified Editor необходима Windows 10 или новее, 16 ГБ оперативной памяти и видеокарта с поддержкой DirectX 11. Скачайте установщик из внутреннего репозитория артефактов и запустите его с правами администратора."),
    ("dds_en", "dds", "The DDS Converter tool batch-converts PNG and TGA textures into DirectDraw Surface format, preserving mipmaps and choosing BC7 compression by default for color textures."),
    ("dds_ru", "dds", "Конвертер DDS выполняет пакетное преобразование текстур PNG и TGA в формат DirectDraw Surface, сохраняя мипмапы и используя сжатие BC7 по умолчанию для цветных текстур."),
    ("pipeline_en", "pipeline", "Asset pipeline validation runs automatically on commit: it checks texture resolution limits, naming conventions, and flags any asset missing a license tag before it reaches the build server."),
    ("pipeline_ru", "pipeline", "Валидация пайплайна ассетов запускается автоматически при коммите: проверяются ограничения разрешения текстур, соглашения об именовании и отсутствие тега лицензии перед попаданием в билд-сервер."),
    ("oncall_en", "oncall", "Engineering on-call rotation is weekly; the on-call engineer is responsible for triaging build failures and responding to production alerts within 15 minutes."),
    ("oncall_ru", "oncall", "Дежурство инженеров меняется еженедельно; дежурный инженер обязан разбирать сбои сборки и реагировать на алерты продакшена в течение 15 минут."),
]

QUERIES = [
    ("q1_en_install", "install", "What are the system requirements to install Unified Editor?"),
    ("q2_ru_dds", "dds", "Какие настройки сжатия используются при конвертации текстур в DDS?"),
    ("q3_en_oncall", "oncall", "How often does the on-call rotation change?"),
    ("q4_ru_pipeline", "pipeline", "Что проверяет валидация ассетов при коммите?"),
    ("q5_cross_en_over_ru_pipeline", "pipeline", "What happens if an asset is missing a license tag?"),
    ("q6_cross_ru_over_en_dds", "dds", "Какой формат сжатия используется по умолчанию для цветных текстур?"),
]

_FILLER = (
    "This section of the internal documentation describes a routine, low-risk "
    "workflow step that engineers follow when preparing content for the shared "
    "pipeline. It exists mainly to give the embedding model a realistic amount "
    "of surrounding context, similar in length to an average documentation "
    "section, so that behavior at longer input lengths can be observed. Please "
    "note that this paragraph intentionally repeats similar phrasing several "
    "times to reach a representative token count without introducing content "
    "that would itself be distinctive or memorable to a retrieval model. "
) * 6

LONG_DOC_A = _FILLER + "In closing, this particular document is actually about texture atlas packing: the build tool groups small textures into a single atlas sheet to reduce draw calls, using a shelf-packing algorithm with a 2-pixel padding border."
LONG_DOC_B = _FILLER + "In closing, this particular document is actually about shader compilation caching: the build tool stores compiled shader permutations keyed by a hash of the source and macro set, to avoid recompiling on every engine startup."


def http_post_json(url: str, payload: dict, timeout: float = 120.0) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def embed(base_url: str, model: str, text: str) -> tuple[list[float], float]:
    start = time.monotonic()
    result = http_post_json(f"{base_url}/api/embeddings", {"model": model, "prompt": text})
    elapsed_ms = (time.monotonic() - start) * 1000
    vector = result.get("embedding")
    if not vector:
        raise RuntimeError(f"no 'embedding' field in response: {result}")
    return vector, elapsed_ms


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: embed_bench.py <ollama-model-name> [ollama-base-url]", file=sys.stderr)
        return 2
    model = sys.argv[1]
    base_url = sys.argv[2] if len(sys.argv) > 2 else "http://localhost:11434"

    print(f"=== embedding benchmark: model={model!r} base_url={base_url!r} ===\n")

    latencies: list[float] = []
    dims: set[int] = set()
    doc_vectors: dict[str, list[float]] = {}

    print(f"embedding {len(DOCS)} corpus docs...")
    for doc_id, _group, text in DOCS:
        try:
            vec, ms = embed(base_url, model, text)
        except (urllib.error.URLError, urllib.error.HTTPError, RuntimeError) as exc:
            print(f"  [ERROR] {doc_id}: {exc}")
            return 1
        doc_vectors[doc_id] = vec
        dims.add(len(vec))
        latencies.append(ms)
        print(f"  {doc_id:14s} dim={len(vec):5d}  {ms:7.1f} ms")

    print(f"\nembedding {len(QUERIES)} queries and ranking against corpus...")
    correct = 0
    reciprocal_ranks: list[float] = []
    for q_id, expected_group, text in QUERIES:
        try:
            qvec, ms = embed(base_url, model, text)
        except (urllib.error.URLError, urllib.error.HTTPError, RuntimeError) as exc:
            print(f"  [ERROR] {q_id}: {exc}")
            return 1
        latencies.append(ms)
        dims.add(len(qvec))
        ranked = sorted(
            ((doc_id, cosine(qvec, dvec)) for doc_id, dvec in doc_vectors.items()),
            key=lambda pair: pair[1],
            reverse=True,
        )
        top_doc_id, top_score = ranked[0]
        top_group = next(g for d, g, _ in DOCS if d == top_doc_id)
        hit = top_group == expected_group
        correct += int(hit)
        rank_of_first_correct = next(
            i for i, (d, _s) in enumerate(ranked, start=1)
            if next(g for dd, g, _ in DOCS if dd == d) == expected_group
        )
        reciprocal_ranks.append(1.0 / rank_of_first_correct)
        status = "OK  " if hit else "MISS"
        print(f"  [{status}] {q_id:28s} -> top={top_doc_id:14s} score={top_score:.3f} (expected group={expected_group})")

    top1_acc = correct / len(QUERIES)
    mrr = sum(reciprocal_ranks) / len(reciprocal_ranks)

    print("\nlong-text truncation probe (two docs, shared prefix, divergent tail)...")
    try:
        vec_a, ms_a = embed(base_url, model, LONG_DOC_A)
        vec_b, ms_b = embed(base_url, model, LONG_DOC_B)
    except (urllib.error.URLError, urllib.error.HTTPError, RuntimeError) as exc:
        print(f"  [ERROR] long-doc probe: {exc}")
        vec_a = vec_b = None
    if vec_a is not None and vec_b is not None:
        latencies.extend([ms_a, ms_b])
        divergence_cos = cosine(vec_a, vec_b)
        approx_prefix_tokens = len(_FILLER) // 4
        print(f"  shared-prefix length ~{len(_FILLER)} chars (~{approx_prefix_tokens} tokens)")
        print(f"  cosine(long_doc_a, long_doc_b) = {divergence_cos:.4f}")
        if divergence_cos > 0.995:
            print("  -> WARNING: near-identical embeddings despite distinct tails; likely truncating"
                  " input before the divergence point (model's context window shorter than this text).")
        else:
            print("  -> tails are distinguished in the embedding; no truncation detected at this length.")

    print("\n=== summary ===")
    print(f"embedding dimension: {sorted(dims)}")
    print(f"latency: avg={sum(latencies)/len(latencies):.1f} ms  max={max(latencies):.1f} ms  n={len(latencies)}")
    print(f"retrieval top-1 accuracy: {correct}/{len(QUERIES)} = {top1_acc:.2f}")
    print(f"retrieval MRR: {mrr:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

4. **Run it:** `python3 /tmp/embed_bench.py "<model>" http://localhost:21434` (the fixed host port `ollama-bench` was published on in step 2). Also capture `docker exec ollama-bench ollama show "<model>"` output (gives parameter size / quantization / context length info reported by Ollama itself) to fold into your report.

5. **Report** the following, plainly, no editorializing:
   - Model name tested.
   - Embedding dimension(s) seen (should be a single consistent number -- flag if not).
   - Avg / max latency per embed call (ms).
   - Retrieval top-1 accuracy and MRR out of 6 queries.
   - Truncation probe result (cosine similarity value + the OK/WARNING line the script prints).
   - Any info from `ollama show` worth noting (declared context length, parameter count, quantization).
   - Any errors encountered (pull failures, HTTP errors, timeouts) verbatim.

Do not modify `.env`, `.env.example`, `docker-compose.yml`, or any `config.yml` -- your job is measurement only. Do not pull or benchmark any model other than the one given in your task input.
