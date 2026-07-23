"""Interactive CLI walkthrough of the RagPipeline (src/libs/pipeline.py),
step by step, against the live stack (Qdrant + AI gateway).

Run via the dedicated `test_search_pipeline` docker-compose service (same
env vars/volumes as mcp-server, no live MCP protocol involved):

    make interactive_test

All step numbering/descriptions and prompt text belong ONLY in this file --
src/libs/pipeline.py exposes RagPipeline.STEP_ORDER (just the method names)
and a post_step hook (name, state before/after, duration) so this script can
report each step's input/output/timing without the pipeline itself knowing
anything about a human being on the other end.
"""

from __future__ import annotations

from typing import Any

from _common.logging_config import configure_logging
from src.libs.pipeline_state import PipelineState
from src.libs.timer import StepTimer
from src.libs.wiring import build_pipeline

_BOLD = "\033[1m"
_RESET = "\033[0m"

STEP_DESCRIPTIONS: dict[str, str] = {
    "rewrite_query": "Rewrite the query into a more formal, documentation-style phrasing (LLM call)",
    "embed_query": "Embed the query text into a dense vector",
    "route_query": "Classify the query as 'point' (specific fact) or 'global' (whole-page overview)",
    "correct_typos": "Fuzzy-correct query tokens against the indexed vocabulary",
    "expand_synonyms": "Expand query tokens with known synonyms",
    "compute_sparse_vector": "Build a sparse (term-frequency) vector for lexical search",
    "build_filters": "Build Qdrant payload filters from title/description/source_path/tags",
    "exact_match_search": "Look up an exact identifier match (config key, CLI flag, dotted path)",
    "hybrid_search": "Run the hybrid dense+sparse search in Qdrant, fused with RRF",
    "cap_chunks_per_source": "Cap how many candidates may come from the same source page",
    "rerank_results": "Rerank candidates with the cross-encoder reranker service",
    "merge_results": "Merge exact-match hits with reranked results, deduplicated",
    "summary_search": "Search the page-level summary index (only for 'global' queries)",
    "build_context": "Assemble the retrieved chunks into a single context block",
    "generate_answer": "Ask the reasoning model to compose an answer grounded in the context",
    "parse_answer": "Parse the model's structured reply into answer/reasoning/sources",
}

# Which PipelineState fields to show as a step's input (read from the state
# *before* the step ran) and output (read from the state *after*) -- mirrors
# what each RagPipeline method in src/libs/pipeline.py actually reads/writes.
STEP_IO: dict[str, tuple[list[str], list[str]]] = {
    "rewrite_query": (["query"], ["search_query"]),
    "embed_query": (["search_query"], ["vector"]),
    "route_query": (["vector"], ["route_type"]),
    "correct_typos": (["search_query"], ["lexical_query"]),
    "expand_synonyms": (["lexical_query"], ["lexical_query"]),
    "compute_sparse_vector": (["lexical_query"], ["sparse_vec"]),
    "build_filters": (["title", "description", "source_path", "tags"], ["query_filter"]),
    "exact_match_search": (["search_query", "query_filter"], ["exact_hits"]),
    "hybrid_search": (["vector", "sparse_vec", "query_filter", "top_k_retrieve"], ["hits"]),
    "cap_chunks_per_source": (["hits"], ["hits"]),
    "rerank_results": (["search_query", "hits"], ["reranked"]),
    "merge_results": (["exact_hits", "reranked"], ["results"]),
    "summary_search": (["route_type", "vector"], ["summary_hits"]),
    "build_context": (["results", "summary_hits"], ["context_text"]),
    "generate_answer": (["context_text", "query"], ["generation_text", "input_tokens", "output_tokens"]),
    "parse_answer": (["generation_text"], ["answer", "reasoning", "sources"]),
}


def _truncate(text: str, limit: int = 300) -> str:
    text = text.replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _fmt_points(points: list) -> str:
    shown = ", ".join(f"{p.id}({p.score:.3f})" for p in points[:5])
    more = "…" if len(points) > 5 else ""
    return f"{len(points)} point(s): {shown}{more}" if points else "0 point(s)"


def _fmt_chunks(chunks: list[dict]) -> str:
    shown = ", ".join(str(c.get("title")) for c in chunks[:5])
    more = "…" if len(chunks) > 5 else ""
    return f"{len(chunks)} item(s): {shown}{more}" if chunks else "0 item(s)"


def _fmt_value(name: str, value: Any) -> str:
    if value is None:
        return "None"
    if name == "vector":
        return f"{len(value)}-dim vector, fingerprint={[round(v, 4) for v in value[:4]]}"
    if name == "sparse_vec":
        return f"sparse vector, {len(value.indices)} nonzero term(s)"
    if name == "query_filter":
        return repr(value)
    if name in ("exact_hits", "hits"):
        return _fmt_points(value)
    if name == "reranked":
        shown = ", ".join(f"{hit.id}={score:.3f}" if score is not None else f"{hit.id}=None" for hit, score in value[:5])
        more = "…" if len(value) > 5 else ""
        return f"{len(value)} pair(s): {shown}{more}" if value else "0 pair(s)"
    if name in ("results", "summary_hits", "context_items"):
        return _fmt_chunks(value)
    if name == "sources":
        return f"{len(value)}: " + "; ".join(value) if value else "0"
    if isinstance(value, str):
        return _truncate(value) if value else '""'
    if isinstance(value, list):
        return ", ".join(str(v) for v in value) if value else "[]"
    return str(value)


def _report_step(index: int, name: str, before: PipelineState, after: PipelineState, duration_ms: float) -> None:
    description = STEP_DESCRIPTIONS.get(name, name)
    input_fields, output_fields = STEP_IO.get(name, ([], []))
    print(f"\n{_BOLD}{index}. {description}{_RESET}  ({duration_ms:.1f}ms)")
    for field_name in input_fields:
        print(f"  in  {field_name}: {_fmt_value(field_name, getattr(before, field_name))}")
    for field_name in output_fields:
        print(f"  out {field_name}: {_fmt_value(field_name, getattr(after, field_name))}")


def _prompt(label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{label}{suffix}: ").strip()
    return value or default


def _prompt_optional(label: str) -> str | None:
    value = input(f"{label} (leave blank to skip): ").strip()
    return value or None


def _prompt_yes_no(label: str, default_yes: bool) -> bool:
    default_label = "Y/n" if default_yes else "y/N"
    value = input(f"{label} [{default_label}]: ").strip().lower()
    if not value:
        return default_yes
    return value in ("y", "yes")


def _build_state(top_k_retrieve: int, top_k_rerank_default: int) -> PipelineState:
    print("\n=== RAG pipeline interactive test ===\n")
    query = _prompt("Query text")
    while not query:
        print("Query text is required.")
        query = _prompt("Query text")
    top_k = _prompt("top_k (reranked chunks to keep)", str(top_k_rerank_default))
    title = _prompt_optional("Filter: title")
    source_path = _prompt_optional("Filter: source_path")
    description = _prompt_optional("Filter: description")
    tags_raw = _prompt_optional("Filter: tags (comma-separated)")
    tags = [t.strip() for t in tags_raw.split(",") if t.strip()] if tags_raw else None
    want_answer = _prompt_yes_no("Run answer_question mode (generate an answer, not just search)?", default_yes=True)

    return PipelineState(
        query=query,
        top_k_retrieve=top_k_retrieve,
        top_k_rerank=max(1, int(top_k)),
        title=title,
        description=description,
        source_path=source_path,
        tags=tags,
        want_answer=want_answer,
        timer=StepTimer(),
    )


def _print_timing_summary(timings: list[tuple[int, str, float]]) -> None:
    total_ms = sum(duration_ms for _, _, duration_ms in timings)
    print("\n=== Step timings ===")
    for index, name, duration_ms in timings:
        pct = (duration_ms / total_ms * 100) if total_ms else 0.0
        print(f"  {index:>2}. {name:<22} {duration_ms:>8.1f}ms  ({pct:4.1f}%)")
    print(f"  {'total':>25} {total_ms:>8.1f}ms")


def _print_summary(state: PipelineState) -> None:
    print("\n=== Result ===")
    print(f"Route type: {state.route_type}")
    print(f"\nRetrieved {len(state.results)} chunk(s):")
    for i, chunk in enumerate(state.results, start=1):
        print(
            f"  [{i}] {chunk['title']} ({chunk['source_path']}) "
            f"retrieval_score={chunk['retrieval_score']:.3f} rerank_score={chunk['rerank_score']}"
        )
    if state.summary_hits:
        print(f"\nSummary hits: {len(state.summary_hits)}")
        # Numbered as a continuation of the results list above, not restarting
        # at 1 -- build_context (pipeline.py) concatenates results +
        # summary_hits into one context_items list and numbers it as a single
        # sequence, and that combined numbering is what generate_answer shows
        # the model and what parse_answer's `sources` cites back. Numbering
        # this list separately from 1 would silently disagree with those ids.
        for i, chunk in enumerate(state.summary_hits, start=len(state.results) + 1):
            print(f"  [{i}] {chunk['title']} ({chunk['source_path']})")

    if state.want_answer:
        print("\n--- Answer ---")
        print(state.answer)
        if state.reasoning:
            print("\n--- Reasoning ---")
            print(state.reasoning)
        if state.sources:
            print("\n--- Sources ---")
            for source in state.sources:
                print(f"  {source}")


def main() -> None:
    configure_logging()
    pipeline, config, _vocab_watcher = build_pipeline()
    state = _build_state(
        top_k_retrieve=config.search_tools.retrieval.top_k_retrieve,
        top_k_rerank_default=config.search_tools.retrieval.top_k_rerank,
    )
    timings: list[tuple[int, str, float]] = []

    def _post_step(index: int, name: str, before: PipelineState, after: PipelineState, duration_ms: float) -> None:
        _report_step(index, name, before, after, duration_ms)
        timings.append((index, name, duration_ms))

    final_state = pipeline.run(state, post_step=_post_step)
    _print_summary(final_state)
    _print_timing_summary(timings)


if __name__ == "__main__":
    main()
