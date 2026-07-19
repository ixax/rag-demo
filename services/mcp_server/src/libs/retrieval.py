"""Retrieval: config schema + the embed -> Qdrant search -> optional rerank
step shared by both MCP tools.
"""

from __future__ import annotations

from typing import Any, Callable

from pydantic import BaseModel, Field
from qdrant_client import QdrantClient
from qdrant_client.models import FieldCondition, Filter, Fusion, FusionQuery, MatchAny, MatchValue, Prefetch

from _common.logging_config import get_logger
from _common.sparse_vector import sparse_vector as compute_sparse_vector

from .query_router import QueryRouter
from .synonyms import expand_query
from .timer import StepTimer
from .typo_correct import correct_tokens

logger = get_logger(__name__)


def _exact_match_result(hit: Any) -> dict:
    payload = hit.payload or {}
    return {
        "title": payload.get("title"),
        "description": payload.get("description"),
        "source_path": payload.get("source_path"),
        "heading": payload.get("heading"),
        "updated": payload.get("lastmod"),
        "text": payload.get("text"),
        # Synthetic top scores -- an exact identifier match bypasses rerank
        # entirely, so there's no real
        # cosine/cross-encoder score to report.
        "retrieval_score": 1.0,
        "rerank_score": 1.0,
    }


# Gate for the exact-match pass: only single-token
# queries that look identifier-shaped (a CLI flag, or contains a separator
# like a config key/dotted method path would) trigger the extra Qdrant
# round-trip -- ordinary prose queries never do.
def _looks_identifier_shaped(query: str) -> bool:
    q = query.strip()
    if not q or " " in q:
        return False
    return q.startswith("--") or any(c in q for c in "_-.")


class RetrievalConfig(BaseModel):
    top_k_retrieve: int = Field(gt=0)
    top_k_rerank: int = Field(gt=0)
    # null | int > 0. Caps how many of the top_k_retrieve candidates may come
    # from the same source_path before reranking. null keeps the old
    # uncapped behavior.
    max_chunks_per_source: int | None = Field(default=None, gt=0)


def retrieve(
    query: str,
    top_k_retrieve: int,
    top_k_rerank: int,
    timer: StepTimer,
    *,
    qdrant_client: QdrantClient,
    collection: str,
    embed_fn: Callable[[str], list[float]],
    reranker_enabled: bool,
    rerank_fn: Callable[[str, list[str]], list[float]] | None = None,
    rerank_confidence_cutoff: float | None = None,
    title: str | None = None,
    description: str | None = None,
    source_path: str | None = None,
    tags: list[str] | None = None,
    max_chunks_per_source: int | None = None,
    typo_vocab: set[str] | None = None,
    typo_threshold: int = 85,
    synonym_table: dict[str, list[str]] | None = None,
    query_router: QueryRouter | None = None,
    route_result: dict | None = None,
    on_step: Callable[[str, float, dict], None] | None = None,
) -> list[dict]:
    """embed -> Qdrant hybrid (dense+sparse, RRF-fused) search -> optional
    rerank. `embed_fn` and `rerank_fn` are injected so this stays agnostic to
    which embedding provider/reranker is configured -- server.py wires those
    up (rerank_fn calls out to the standalone reranker HTTP service, see
    libs/reranker.py).

    `title`/`description`/`source_path`/`tags`, when given, exact-match
    filter the vector search itself (not a post-filter) via the keyword
    payload indexes ingest.py creates -- narrows to a known page/category
    instead of relying on semantic similarity alone. `tags` matches if the
    chunk has any of the given tags (MatchAny), not all of them.

    `max_chunks_per_source`, when given, caps how many of the top
    `top_k_retrieve` candidates may come from the same `source_path` before
    reranking -- keeps one large page from crowding out other relevant
    sources.

    `on_step`, when given, is called synchronously right after each timed
    stage finishes -- (step_name, duration_ms, characteristics). This runs
    on whatever thread `retrieve()` itself runs on: server.py's callers run
    `retrieve()` in a worker thread and hop back to the event loop inside
    the callback (`anyio.from_thread.run`) to push an MCP progress
    notification as each step completes, instead of only after the whole
    call returns. `characteristics` is a small dict of counts/dimensions
    (e.g. {"dim": 1024}, {"candidates": 12}) -- never chunk text or scores,
    so it's safe to put in a progress message a client displays live."""
    with timer("embed_query"):
        vector = embed_fn(query)
    # Full vectors are 300-1024+ floats -- not human-readable and not worth
    # logging in full. A dimension + short fingerprint (first 4 components)
    # is enough to confirm embedding actually ran and roughly what it
    # produced, without dumping an unreadable wall of numbers per request.
    logger.info(
        "embed_query query=%r dim=%d fingerprint=%s",
        query,
        len(vector),
        [round(v, 4) for v in vector[:4]],
    )
    if on_step:
        on_step("embed_query", timer.trace[-1]["duration_ms"], {"dim": len(vector)})

    # Point-vs-global classification -- reuses
    # the vector just computed above, no extra embed call. Doesn't change
    # this function's own behavior yet; `route_result`, when given, lets the
    # caller (server.py) read the classification back out to decide whether
    # to also query a summary index.
    if route_result is not None:
        # Stashed unconditionally (not just when query_router is set) so a
        # caller that later wants to query the summary index can reuse
        # this vector without a second embed
        # call, regardless of whether routing itself ran.
        route_result["vector"] = vector
    if query_router is not None:
        with timer("route"):
            route_type = query_router.classify(vector)
        if route_result is not None:
            route_result["type"] = route_type
        if on_step:
            on_step("route", timer.trace[-1]["duration_ms"], {"type": route_type})

    # typo correction + synonym expansion feed only the sparse/lexical
    # branch -- dense embedding of `query` above already tolerates minor
    # typos/wording differences, so mutating the text used for `embed_fn`
    # would just dilute the dense vector for no benefit.
    lexical_query = query
    if typo_vocab:
        with timer("typo_correct"):
            lexical_query, corrections = correct_tokens(lexical_query, typo_vocab, typo_threshold)
        if lexical_query != query:
            logger.info("typo_correct query=%r corrected=%r", query, lexical_query)
        if on_step:
            on_step("typo_correct", timer.trace[-1]["duration_ms"], {"corrections": corrections})
    if synonym_table:
        with timer("synonym_expand"):
            lexical_query, terms_added = expand_query(lexical_query, synonym_table)
        if on_step:
            on_step("synonym_expand", timer.trace[-1]["duration_ms"], {"terms_added": terms_added})

    sparse_vec = compute_sparse_vector(lexical_query)

    conditions: list[FieldCondition] = []
    if title is not None:
        conditions.append(FieldCondition(key="title", match=MatchValue(value=title)))
    if description is not None:
        conditions.append(FieldCondition(key="description", match=MatchValue(value=description)))
    if source_path is not None:
        conditions.append(FieldCondition(key="source_path", match=MatchValue(value=source_path)))
    if tags is not None:
        conditions.append(FieldCondition(key="tags", match=MatchAny(any=tags)))
    # mypy: Filter.must's declared type is a broader Condition union: list is
    # invariant, so list[FieldCondition] doesn't satisfy it even though every
    # element is a valid Condition at runtime.
    query_filter = Filter(must=conditions) if conditions else None  # type: ignore[arg-type]

    # Exact-match pass: for identifier-shaped
    # queries only (see _looks_identifier_shaped) -- skips the extra Qdrant
    # round-trip entirely for ordinary prose. Matches ingest.py's
    # `identifiers` payload field (config keys, CLI flags, dotted method
    # paths extracted from code chunks), which is exact/case-normalized, not
    # a semantic match -- so these hits bypass rerank and are prepended to
    # the final results with a synthetic top score, in addition to (not
    # instead of) the normal hybrid search below.
    exact_hits: list[Any] = []
    if _looks_identifier_shaped(query):
        identifier_conditions = [*conditions, FieldCondition(key="identifiers", match=MatchValue(value=query.strip().lower()))]
        with timer("exact_match"):
            exact_hits, _ = qdrant_client.scroll(
                collection_name=collection,
                scroll_filter=Filter(must=identifier_conditions),  # type: ignore[arg-type]
                limit=top_k_retrieve,
            )
        if on_step:
            on_step("exact_match", timer.trace[-1]["duration_ms"], {"hits": len(exact_hits)})

    with timer("qdrant_search"):
        # ingest.py's collection uses named vectors ("dense" + a "sparse"
        # term-frequency lexical vector, IDF-weighted server-side). Query
        # API fuses both branches with
        # RRF: dense catches semantic similarity, sparse catches exact-term
        # matches (config keys, method names, error codes) dense alone
        # misses. The filter is applied per-prefetch so it narrows each
        # branch's own candidate pool, not just the fused result.
        response = qdrant_client.query_points(
            collection_name=collection,
            prefetch=[
                Prefetch(query=vector, using="dense", limit=top_k_retrieve, filter=query_filter),
                Prefetch(query=sparse_vec, using="sparse", limit=top_k_retrieve, filter=query_filter),
            ],
            query=FusionQuery(fusion=Fusion.RRF),
            limit=top_k_retrieve,
        )
    hits = response.points
    if on_step:
        on_step("qdrant_search", timer.trace[-1]["duration_ms"], {"candidates": len(hits)})
    if not hits:
        return [_exact_match_result(hit) for hit in exact_hits]

    if max_chunks_per_source is not None:
        capped = []
        per_source_count: dict[str, int] = {}
        for hit in hits:
            source = str((hit.payload or {}).get("source_path") or "")
            count = per_source_count.get(source, 0)
            if count >= max_chunks_per_source:
                continue
            per_source_count[source] = count + 1
            capped.append(hit)
        hits = capped

    # reranker_enabled: false skips this step entirely -- results keep plain
    # Qdrant cosine-similarity order and rerank_score is null, instead of
    # calling out to the reranker service.
    reranked: list[tuple[Any, float | None]]
    if reranker_enabled:
        if rerank_fn is None:
            raise ValueError("rerank_fn is required when reranker_enabled is True")
        texts = [(hit.payload or {}).get("text", "") for hit in hits]
        try:
            with timer("rerank"):
                scores = rerank_fn(query, texts)
        except Exception as exc:
            # Reranker down/unreachable/timed out -- fall back to plain
            # Qdrant order (same shape as reranker_enabled: false) instead of
            # failing the whole request over a non-essential ranking step.
            logger.warning("reranker call failed, falling back to vector-similarity order: %s", exc)
            reranked = [(hit, None) for hit in hits[:top_k_rerank]]
        else:
            ranked = sorted(zip(hits, scores), key=lambda pair: pair[1], reverse=True)
            # Confidence-gated cutoff: if the top hit clears
            # rerank_confidence_cutoff, drop everything below it -- a
            # confidently-relevant match exists, so weaker ones are noise
            # (this is what let e.g. an unrelated "Commit" chunk sit at
            # rerank_score 0.85 next to the actually-relevant "Launch" chunk
            # at 0.98 and still get fed to the generator as if comparably
            # relevant). Skipped when the top score itself doesn't clear the
            # cutoff -- a mediocre top match shouldn't be reduced to nothing.
            if ranked and rerank_confidence_cutoff is not None and ranked[0][1] >= rerank_confidence_cutoff:
                ranked = [pair for pair in ranked if pair[1] >= rerank_confidence_cutoff]
            reranked = list(ranked[:top_k_rerank])
        if on_step:
            on_step("rerank", timer.trace[-1]["duration_ms"], {"kept": len(reranked)})
    else:
        reranked = [(hit, None) for hit in hits[:top_k_rerank]]

    results = []
    seen_ids = {hit.id for hit, _ in reranked}
    # Exact-match hits go first, deduped against the hybrid-search results
    # they may overlap with (a code chunk can rank on its own merit too).
    for exact_hit in exact_hits:
        if exact_hit.id in seen_ids:
            continue
        seen_ids.add(exact_hit.id)
        results.append(_exact_match_result(exact_hit))
    for hit, rerank_score in reranked:
        payload = hit.payload or {}
        results.append(
            {
                "title": payload.get("title"),
                "description": payload.get("description"),
                "source_path": payload.get("source_path"),
                "heading": payload.get("heading"),
                "updated": payload.get("lastmod"),
                "text": payload.get("text"),
                "retrieval_score": float(hit.score),
                "rerank_score": float(rerank_score) if rerank_score is not None else None,
            }
        )
    return results


def retrieve_summary(
    query_vector: list[float],
    limit: int,
    timer: StepTimer,
    *,
    qdrant_client: QdrantClient,
    collection: str,
    on_step: Callable[[str, float, dict], None] | None = None,
) -> list[dict]:
    """Plain dense search (no sparse/fusion -- summaries are natural-language
    prose, not code/identifiers) against the page-level summary collection
    ingest.py writes. Only called when
    `retrieve()`'s `route_result["type"]` came back "global" -- callers reuse
    the query vector `retrieve()` already computed via `route_result`
    instead of embedding again."""
    with timer("summary_search"):
        response = qdrant_client.query_points(
            collection_name=collection,
            query=query_vector,
            limit=limit,
        )
    hits = response.points
    if on_step:
        on_step("summary_search", timer.trace[-1]["duration_ms"], {"hits": len(hits)})
    results = []
    for hit in hits:
        payload = hit.payload or {}
        results.append(
            {
                "title": payload.get("title"),
                "description": payload.get("description"),
                "source_path": payload.get("source_path"),
                "heading": None,
                "updated": None,
                "text": payload.get("summary_text"),
                "retrieval_score": float(hit.score),
                "rerank_score": None,
                "kind": "summary",
            }
        )
    return results


def retrieval_status(
    results: list[dict],
    *,
    reranker_enabled: bool,
    rerank_confidence_cutoff: float | None,
) -> str:
    """Cheap, log-only status classification for a completed retrieve() call
    -- foundation for failed-query logging and, eventually, a synonym
    table mined from queries that came back low_confidence/empty. Not
    used for any behavioral decision here, only logged."""
    if not results:
        return "empty"
    if reranker_enabled and rerank_confidence_cutoff is not None:
        top_rerank_score = results[0]["rerank_score"]
        if top_rerank_score is not None and top_rerank_score < rerank_confidence_cutoff:
            return "low_confidence"
    return "ok"
