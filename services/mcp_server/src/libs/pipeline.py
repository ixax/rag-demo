"""RagPipeline: the RAG chain (query rewrite -> embed -> route -> typo
correct -> synonym expand -> sparse vector -> filters -> exact match ->
hybrid Qdrant search -> near-duplicate dedup -> per-source cap -> rerank ->
merge -> summary search -> [answer_question only] build context -> generate
-> parse answer) as one
class, one method per step.

Every step method has the signature `(self, state: PipelineState) ->
PipelineState`: it reads what it needs off `state` and writes its own
output back onto the same object. When a step's config gates it off (e.g.
query_rewrite disabled, no typo vocab, reranker disabled, want_answer=False)
the method returns `state` with its field(s) already set to whatever value
the next step expects in the "this step did nothing" case -- no special
casing lives downstream. `run()` just walks `STEP_ORDER` and calls each
method in turn, threading `state` forward.

server.py builds the dependencies (Qdrant/embedding/reasoning/reranker
clients, the validated config, the query router, vocab/synonym lookups) and
constructs one `RagPipeline` at startup; interactive_test.py does the same
thing for its own standalone process. Neither this module nor
pipeline_state.py reads env vars or config.yml directly.
"""

from __future__ import annotations

import copy
import time
from typing import Any, Callable

from qdrant_client import QdrantClient
from qdrant_client.models import FieldCondition, Filter, Fusion, FusionQuery, MatchAny, MatchValue, Prefetch

from _common.clients.reasoning_client import ReasoningClient
from _common.logging_config import get_logger
from _common.sparse_vector import sparse_vector as compute_sparse_vector

from .config import SearchToolsConfig, parse_structured_answer
from .pipeline_state import PipelineState
from .query_rewrite import rewrite_query as _call_rewrite_query
from .query_router import QueryRouter
from .retrieval import _cosine_similarity, _dense_vector, _exact_match_result, _looks_identifier_shaped
from .synonyms import expand_query
from .typo_correct import correct_tokens

logger = get_logger(__name__)


class RagPipeline:
    STEP_ORDER: list[str] = [
        "rewrite_query",
        "embed_query",
        "route_query",
        "correct_typos",
        "expand_synonyms",
        "compute_sparse_vector",
        "build_filters",
        "exact_match_search",
        "hybrid_search",
        "dedup_near_duplicates",
        "cap_chunks_per_source",
        "rerank_results",
        "merge_results",
        "summary_search",
        "build_context",
        "generate_answer",
        "parse_answer",
    ]

    def __init__(
        self,
        *,
        qdrant_client: QdrantClient,
        collection: str,
        summary_collection: str,
        embed_fn: Callable[[str], list[float]],
        rerank_fn: Callable[[str, list[str]], list[float]] | None,
        reasoning_client: ReasoningClient,
        query_router: QueryRouter | None,
        typo_vocab_fn: Callable[[], set[str]] | None,
        synonym_table: dict[str, list[str]],
        config: SearchToolsConfig,
    ) -> None:
        self.qdrant_client = qdrant_client
        self.collection = collection
        self.summary_collection = summary_collection
        self.embed_fn = embed_fn
        self.rerank_fn = rerank_fn
        self.reasoning_client = reasoning_client
        self.query_router = query_router
        self.typo_vocab_fn = typo_vocab_fn
        self.synonym_table = synonym_table
        self.config = config

    def run(
        self,
        state: PipelineState,
        pre_step: Callable[[int, str, PipelineState], None] | None = None,
        post_step: Callable[[int, str, PipelineState, PipelineState, float], None] | None = None,
    ) -> PipelineState:
        for i, name in enumerate(self.STEP_ORDER, start=1):
            if pre_step:
                pre_step(i, name, state)
            # Shallow copy is enough for a before/after diff: every step
            # reassigns the fields it changes to a new object rather than
            # mutating one in place (see module docstring), so the copy's
            # references stay untouched by the step call below.
            state_before = copy.copy(state)
            start = time.perf_counter()
            state = getattr(self, name)(state)
            duration_ms = (time.perf_counter() - start) * 1000
            if post_step:
                post_step(i, name, state_before, state, duration_ms)
        return state

    # -- steps ---------------------------------------------------------

    def rewrite_query(self, state: PipelineState) -> PipelineState:
        if not self.config.query_rewrite.enabled:
            state.search_query = state.query
            return state
        with state.timer("query_rewrite"):
            rewritten = _call_rewrite_query(
                state.query,
                self.reasoning_client,
                self.config.query_rewrite.model,
                self.config.query_rewrite.system_prompt,
            )
        if rewritten != state.query:
            logger.info("query_rewrite query=%r rewritten=%r", state.query, rewritten)
        state.search_query = rewritten
        if state.on_step:
            state.on_step("query_rewrite", state.timer.trace[-1]["duration_ms"], {"changed": rewritten != state.query})
        return state

    def embed_query(self, state: PipelineState) -> PipelineState:
        with state.timer("embed_query"):
            vector = self.embed_fn(state.search_query)
        logger.info(
            "embed_query query=%r dim=%d fingerprint=%s",
            state.search_query,
            len(vector),
            [round(v, 4) for v in vector[:4]],
        )
        state.vector = vector
        if state.on_step:
            state.on_step("embed_query", state.timer.trace[-1]["duration_ms"], {"dim": len(vector)})
        return state

    def route_query(self, state: PipelineState) -> PipelineState:
        if self.query_router is None:
            state.route_type = "point"
            return state
        with state.timer("route"):
            route_type = self.query_router.classify(state.vector)
        state.route_type = route_type
        if state.on_step:
            state.on_step("route", state.timer.trace[-1]["duration_ms"], {"type": route_type})
        return state

    def correct_typos(self, state: PipelineState) -> PipelineState:
        if not self.config.typo_correction.enabled:
            state.lexical_query = state.search_query
            return state
        typo_vocab = self.typo_vocab_fn() if self.typo_vocab_fn else set()
        if not typo_vocab:
            state.lexical_query = state.search_query
            return state
        with state.timer("typo_correct"):
            lexical_query, corrections = correct_tokens(state.search_query, typo_vocab, self.config.typo_correction.threshold)
        if lexical_query != state.search_query:
            logger.info("typo_correct query=%r corrected=%r", state.search_query, lexical_query)
        state.lexical_query = lexical_query
        if state.on_step:
            state.on_step("typo_correct", state.timer.trace[-1]["duration_ms"], {"corrections": corrections})
        return state

    def expand_synonyms(self, state: PipelineState) -> PipelineState:
        if not self.config.synonym_expansion.enabled or not self.synonym_table:
            return state
        with state.timer("synonym_expand"):
            lexical_query, terms_added = expand_query(state.lexical_query, self.synonym_table)
        state.lexical_query = lexical_query
        if state.on_step:
            state.on_step("synonym_expand", state.timer.trace[-1]["duration_ms"], {"terms_added": terms_added})
        return state

    def compute_sparse_vector(self, state: PipelineState) -> PipelineState:
        state.sparse_vec = compute_sparse_vector(state.lexical_query)
        return state

    def build_filters(self, state: PipelineState) -> PipelineState:
        conditions: list[FieldCondition] = []
        if state.title is not None:
            conditions.append(FieldCondition(key="title", match=MatchValue(value=state.title)))
        if state.description is not None:
            conditions.append(FieldCondition(key="description", match=MatchValue(value=state.description)))
        if state.source_path is not None:
            conditions.append(FieldCondition(key="source_path", match=MatchValue(value=state.source_path)))
        if state.tags is not None:
            conditions.append(FieldCondition(key="tags", match=MatchAny(any=state.tags)))
        # mypy: Filter.must's declared type is a broader Condition union: list
        # is invariant, so list[FieldCondition] doesn't satisfy it even
        # though every element is a valid Condition at runtime.
        state.query_filter = Filter(must=conditions) if conditions else None  # type: ignore[arg-type]
        return state

    def exact_match_search(self, state: PipelineState) -> PipelineState:
        if not _looks_identifier_shaped(state.search_query):
            state.exact_hits = []
            return state
        conditions = list(state.query_filter.must) if state.query_filter else []  # type: ignore[arg-type]
        identifier_conditions = [*conditions, FieldCondition(key="identifiers", match=MatchValue(value=state.search_query.strip().lower()))]
        with state.timer("exact_match"):
            exact_hits, _ = self.qdrant_client.scroll(
                collection_name=self.collection,
                scroll_filter=Filter(must=identifier_conditions),  # type: ignore[arg-type]
                limit=state.top_k_retrieve,
            )
        state.exact_hits = exact_hits
        if state.on_step:
            state.on_step("exact_match", state.timer.trace[-1]["duration_ms"], {"hits": len(exact_hits)})
        return state

    def hybrid_search(self, state: PipelineState) -> PipelineState:
        with state.timer("qdrant_search"):
            response = self.qdrant_client.query_points(
                collection_name=self.collection,
                prefetch=[
                    Prefetch(query=state.vector, using="dense", limit=state.top_k_retrieve, filter=state.query_filter),
                    Prefetch(query=state.sparse_vec, using="sparse", limit=state.top_k_retrieve, filter=state.query_filter),
                ],
                query=FusionQuery(fusion=Fusion.RRF),
                limit=state.top_k_retrieve,
                # Needed by dedup_near_duplicates' cosine-similarity check
                # below -- skip the (unused) sparse vector to keep the
                # response small.
                with_vectors=["dense"],
            )
        state.hits = response.points
        if state.on_step:
            state.on_step("qdrant_search", state.timer.trace[-1]["duration_ms"], {"candidates": len(state.hits)})
        return state

    def dedup_near_duplicates(self, state: PipelineState) -> PipelineState:
        threshold = self.config.retrieval.dedup_similarity_threshold
        if threshold is None or not state.hits:
            return state
        with state.timer("dedup_near_duplicates"):
            kept: list[Any] = []
            kept_vectors: list[list[float]] = []
            dropped = 0
            for hit in state.hits:
                vector = _dense_vector(hit)
                # A hit fused in via the sparse-only leg can carry no dense
                # vector -- always keep it, since similarity can't be checked.
                if vector is not None and any(_cosine_similarity(vector, kv) >= threshold for kv in kept_vectors):
                    dropped += 1
                    continue
                kept.append(hit)
                if vector is not None:
                    kept_vectors.append(vector)
            state.hits = kept
        if state.on_step:
            state.on_step("dedup_near_duplicates", state.timer.trace[-1]["duration_ms"], {"dropped": dropped})
        return state

    def cap_chunks_per_source(self, state: PipelineState) -> PipelineState:
        max_per_source = self.config.retrieval.max_chunks_per_source
        if max_per_source is None or not state.hits:
            return state
        capped = []
        per_source_count: dict[str, int] = {}
        for hit in state.hits:
            source = str((hit.payload or {}).get("source_path") or "")
            count = per_source_count.get(source, 0)
            if count >= max_per_source:
                continue
            per_source_count[source] = count + 1
            capped.append(hit)
        state.hits = capped
        return state

    def rerank_results(self, state: PipelineState) -> PipelineState:
        if not state.hits:
            state.reranked = []
            return state
        if not self.config.reranker.enabled:
            state.reranked = [(hit, None) for hit in state.hits[: state.top_k_rerank]]
            return state
        if self.rerank_fn is None:
            raise ValueError("rerank_fn is required when reranker_enabled is True")
        texts = [(hit.payload or {}).get("text", "") for hit in state.hits]
        try:
            with state.timer("rerank"):
                scores = self.rerank_fn(state.search_query, texts)
        except Exception as exc:
            # Reranker down/unreachable/timed out -- fall back to plain
            # Qdrant order (same shape as reranker disabled) instead of
            # failing the whole request over a non-essential ranking step.
            logger.warning("reranker call failed, falling back to vector-similarity order: %s", exc)
            state.reranked = [(hit, None) for hit in state.hits[: state.top_k_rerank]]
            return state
        ranked = sorted(zip(state.hits, scores), key=lambda pair: pair[1], reverse=True)
        cutoff = self.config.reranker.confidence_cutoff
        # Confidence-gated cutoff: if the top hit clears cutoff, drop
        # everything below it -- skipped when the top score itself doesn't
        # clear the cutoff, so a mediocre top match isn't reduced to nothing.
        if ranked and cutoff is not None and ranked[0][1] >= cutoff:
            ranked = [pair for pair in ranked if pair[1] >= cutoff]
        state.reranked = list(ranked[: state.top_k_rerank])
        if state.on_step:
            state.on_step("rerank", state.timer.trace[-1]["duration_ms"], {"kept": len(state.reranked)})
        return state

    def merge_results(self, state: PipelineState) -> PipelineState:
        results = []
        seen_ids = {hit.id for hit, _ in state.reranked}
        # Exact-match hits go first, deduped against the hybrid-search
        # results they may overlap with.
        for exact_hit in state.exact_hits:
            if exact_hit.id in seen_ids:
                continue
            seen_ids.add(exact_hit.id)
            results.append(_exact_match_result(exact_hit))
        for hit, rerank_score in state.reranked:
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
        state.results = results
        return state

    def summary_search(self, state: PipelineState) -> PipelineState:
        if state.route_type != "global":
            state.summary_hits = []
            return state
        with state.timer("summary_search"):
            response = self.qdrant_client.query_points(
                collection_name=self.summary_collection,
                query=state.vector,
                limit=self.config.summary.limit,
            )
        hits = response.points
        if state.on_step:
            state.on_step("summary_search", state.timer.trace[-1]["duration_ms"], {"hits": len(hits)})
        summary_hits = []
        for hit in hits:
            payload = hit.payload or {}
            summary_hits.append(
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
        state.summary_hits = summary_hits
        return state

    def build_context(self, state: PipelineState) -> PipelineState:
        if not state.want_answer:
            return state
        context_items = state.results + state.summary_hits
        context_text = "\n\n".join(
            (self.config.summary.source_template if c.get("kind") == "summary" else self.config.source_template).format(
                id=i, title=c["title"], path=c["source_path"], updated=c["updated"] or "unknown", text=c["text"]
            )
            for i, c in enumerate(context_items, start=1)
        )
        state.context_items = context_items
        state.context_text = context_text
        return state

    def generate_answer(self, state: PipelineState) -> PipelineState:
        if not state.want_answer or not state.context_items:
            return state
        profile = self.config.generation
        user_content = profile.user_prompt_template.format(context=state.context_text, query=state.query)
        with state.timer("generate"):
            result = self.reasoning_client.generate(
                self.config.reasoning.model,
                profile.system_prompt,
                user_content,
                options=profile.options,
                response_schema=profile.response_schema,
            )
        state.generation_text = result.text
        state.input_tokens = result.input_tokens
        state.output_tokens = result.output_tokens
        if state.on_step:
            state.on_step("generate", state.timer.trace[-1]["duration_ms"], {"answer_chars": len(result.text)})
        return state

    def parse_answer(self, state: PipelineState) -> PipelineState:
        if not state.want_answer:
            return state
        if not state.context_items:
            state.answer = "No relevant documents were found in the knowledge base for this query."
            state.reasoning = None
            state.sources = []
            return state
        reasoning, answer, source_ids = parse_structured_answer(state.generation_text)
        sources = [
            f'[{sid}] {state.context_items[sid - 1]["title"]} ({state.context_items[sid - 1]["source_path"]})'
            for sid in source_ids
            if 1 <= sid <= len(state.context_items)
        ]
        state.reasoning = reasoning
        state.answer = answer
        state.sources = sources
        return state
