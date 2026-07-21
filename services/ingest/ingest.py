"""Ingest Hugo content from CONTENT_DIR into Qdrant.

Walks the content tree, parses Hugo front matter, strips Hugo shortcodes /
raw HTML noise, chunks each page by section (## / ### headings), embeds
each chunk via the AI gateway's embedding model, and upserts everything into
a Qdrant collection.

Two modes, controlled by FORCE_INGEST:
- FORCE_INGEST=true: full rebuild -- drop and recreate the collection from
  every file (also triggered automatically if the collection is missing).
- FORCE_INGEST=false: incremental -- a manifest of per-file content hashes
  (persisted at STATE_DIR/manifest.json) is used to detect which files are
  new, changed, or removed since the last run; only those files' chunks are
  re-embedded and upserted (stale points for changed/removed files are
  deleted first), unchanged files are skipped entirely.

A lock file at STATE_DIR/ingest.lock prevents two runs from racing against
the same manifest/collection; a run refuses to start while another one holds
the lock (see acquire_lock/release_lock).
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, cast

import httpx
import yaml
from environs import Env
from pydantic import BaseModel, Field
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Datatype,
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    Modifier,
    PayloadSchemaType,
    PointStruct,
    SparseIndexParams,
    SparseVectorParams,
    VectorParams,
)

from _common.config import load_yaml_config
from _common.logging_config import configure_logging, get_logger
from _common.clients.embedding_client import EmbeddingClient
from _common.clients.reasoning_client import ReasoningClient
from _common.sparse_vector import sparse_vector as compute_sparse_vector
from _common.tracing import configure_tracing, get_tracer
from _common.vocab import extract_terms, save_vocab

# env.str(name)/env.bool(name) raise a clear environs.EnvError if the
# variable is unset -- no hand-rolled _require_env needed (was copy-pasted
# between this file and services/mcp_server/src/server.py).
env = Env()

# No defaults here on purpose -- defaults live in docker-compose.yml
# (via ${VAR:-default}); this script just requires the values to be set.
# Infra/credentials/run-mode only -- pipeline tuning knobs (paths, chunking,
# batch size) live in config.yml instead, see below.
QDRANT_URL = env.str("QDRANT_URL")
QDRANT_COLLECTION = env.str("QDRANT_COLLECTION")
AI_GATEWAY_URL = env.str("AI_GATEWAY_URL")
AI_GATEWAY_AUTH_KEY = env.str("AI_GATEWAY_AUTH_KEY")
AI_GATEWAY_AUTH_HEADER = env.str("AI_GATEWAY_AUTH_HEADER")
AI_GATEWAY_AUTH_VALUE_TEMPLATE = env.str("AI_GATEWAY_AUTH_VALUE_TEMPLATE")
AI_GATEWAY_EMBEDDINGS_MODEL = env.str("AI_GATEWAY_EMBEDDINGS_MODEL")
# Reasoning backend for the summary index -- same model/gateway
# mcp-server's answer_question uses.
AI_GATEWAY_REASONING_MODEL = env.str("AI_GATEWAY_REASONING_MODEL")
FORCE_INGEST = env.bool("FORCE_INGEST")

configure_logging()
logger = get_logger(__name__)

configure_tracing("ingest")
tracer = get_tracer(__name__)

# Pipeline tuning knobs (content/state paths, chunking, upsert batch size)
# live in config.yml, not the environment -- see that file for the full
# field reference. `make ingest`/`make ingest-force` always rebuild the
# image, so editing this file just needs a normal run, no separate rebuild
# step. Reading/validating config.yml itself is _common.config's
# load_yaml_config (shared with mcp-server and the reranker service).


class _PathsConfig(BaseModel):
    content_dir: str
    state_dir: str


class _ChunkingConfig(BaseModel):
    max_chars: int = Field(gt=0)
    min_chars: int = Field(gt=0)
    overlap_chars: int = Field(ge=0)


class _SummaryConfig(BaseModel):
    system_prompt: str
    timeout: float = Field(gt=0)


class _FeaturesConfig(BaseModel):
    normalize_windows_paths: bool = False


class IngestConfig(BaseModel):
    paths: _PathsConfig
    chunking: _ChunkingConfig
    upsert_batch_size: int = Field(gt=0)
    embedding_timeout: float = Field(gt=0)
    summary: _SummaryConfig
    features: _FeaturesConfig = _FeaturesConfig()


_config = load_yaml_config(Path("config.yml"), IngestConfig)

CONTENT_DIR = Path(_config.paths.content_dir)
STATE_DIR = Path(_config.paths.state_dir)
CHUNK_MAX_CHARS = _config.chunking.max_chars
CHUNK_MIN_CHARS = _config.chunking.min_chars
# Clamp defensively -- a misconfigured overlap >= max_chars would make _pack's
# "current" never shrink, growing every chunk to max_chars and looping.
CHUNK_OVERLAP_CHARS = min(_config.chunking.overlap_chars, CHUNK_MAX_CHARS - 1)
UPSERT_BATCH_SIZE = _config.upsert_batch_size
EMBEDDING_TIMEOUT = _config.embedding_timeout
SUMMARY_SYSTEM_PROMPT = _config.summary.system_prompt
SUMMARY_TIMEOUT = _config.summary.timeout
NORMALIZE_WINDOWS_PATHS = _config.features.normalize_windows_paths

# Fixed suffix, not independently configurable -- mcp-server's retrieval
# code (libs/retrieval.py's retrieve_summary) queries this exact name
# alongside QDRANT_COLLECTION.
QDRANT_SUMMARY_COLLECTION = f"{QDRANT_COLLECTION}_summaries"

MANIFEST_PATH = STATE_DIR / "manifest.json"
# vocab.json is the query-side typo-correction vocabulary mcp-server reads
# (see services/mcp_server/src/libs/typo_correct.py) -- aggregated from every manifest entry's
# "vocab_terms" at the end of each run, so it stays in sync with whatever's
# actually indexed without a separate full-corpus scan.
VOCAB_PATH = STATE_DIR / "vocab.json"

# Filesystem lock so two `make ingest` / `make ingest-force` runs (each a
# separate container sharing the STATE_DIR volume) can't clobber each other.
# Just a plain lock file for now -- fine for the single-host case this repo
# runs in; revisit if ingest ever runs from more than one host at once.
LOCK_PATH = STATE_DIR / "ingest.lock"

# Deterministic namespace so point IDs are stable across runs (path + chunk index).
POINT_NAMESPACE = uuid.UUID("6f9a1b2e-2e0a-4a5f-9b0e-8a5b1a0e5c11")

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?\n)---\s*\n?", re.DOTALL)
HEADING_RE = re.compile(r"^(#{2,3})\s+(.*?)\s*$", re.MULTILINE)


# --------------------------------------------------------------------------
# Front matter
# --------------------------------------------------------------------------


@dataclass
class Page:
    rel_path: str
    meta: dict[str, Any]
    body: str


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    raw_meta = match.group(1)
    body = text[match.end() :]
    try:
        meta = yaml.safe_load(raw_meta) or {}
    except yaml.YAMLError:
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    return meta, body


def load_page(path: Path) -> Page:
    text = path.read_text(encoding="utf-8", errors="replace")
    meta, body = split_frontmatter(text)
    rel_path = str(path.relative_to(CONTENT_DIR))
    return Page(rel_path=rel_path, meta=meta, body=body)


def derive_tags(rel_path: str, meta: dict[str, Any]) -> list[str]:
    tags: list[str] = list(Path(rel_path).parent.parts)
    categories = meta.get("categories")
    if isinstance(categories, list):
        tags.extend(str(c) for c in categories)
    elif isinstance(categories, str):
        tags.append(categories)
    # de-dupe, keep order
    seen: set[str] = set()
    ordered = []
    for tag in tags:
        if tag and tag not in seen:
            seen.add(tag)
            ordered.append(tag)
    return ordered


# --------------------------------------------------------------------------
# Shortcode / HTML cleaning
# --------------------------------------------------------------------------

# Shortcodes whose content is purely visual (images, videos, page listings,
# annotation overlays) and carries no useful text for retrieval.
_DROP_PAIRED = ("interactive-image", "interactive-image-editor")
_DROP_SELF = (
    "media",
    "video",
    "icon",
    "attachment",
    "children",
    "include-page",
    "filter",
    "jiraissue",
)
# Shortcodes that only wrap text -- unwrap and keep the inner content.
_UNWRAP = ("panel", "stack", "tab", "tabs", "label", "table", "details")


def _html_table_to_text(fragment: str) -> str:
    # Strip Hugo shortcodes first -- the generic HTML tag stripper below
    # would otherwise mistake e.g. "{{<media src=...>}}" for an HTML tag
    # (matching just the "<media src=...>" part) and leave "{{}}" behind.
    text = re.sub(r"\{\{[<%].*?[%>]\}\}", "", fragment, flags=re.DOTALL)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</tr\s*>", "\n", text)
    text = re.sub(r"(?i)</t[dh]\s*>", " | ", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    lines = []
    for line in text.splitlines():
        cleaned = line.strip(" |").strip()
        if cleaned:
            lines.append(cleaned)
    return "\n".join(lines)


_FENCED_CODE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")


def _normalize_windows_paths(text: str) -> str:
    """Rewrites backslashes to forward slashes inside inline `code` spans
    (fenced ``` blocks are left untouched -- those can be real code, where a
    backslash may be meaningful). Gated by features.normalize_windows_paths
    in config.yml: a doc's literal Windows-style path (e.g.
    `\\win64\\unifiededitor.exe`), reproduced verbatim by the generation
    model into a JSON-schema-constrained reply, corrupts under
    parse_structured_answer's json.loads -- grammar-constrained JSON sampling
    only allows a bare backslash to be followed by one of \"\\/bfnrtu, so a
    literal single backslash the model didn't double-escape gets silently
    decoded as an unintended escape (\\b/\\u...) instead of raising. Fixing
    it here removes the backslash from the model's context entirely, instead
    of relying on the model to escape correctly."""
    fences: list[str] = []

    def _protect_fence(m: re.Match) -> str:
        fences.append(m.group())
        return f"\x00FENCE{len(fences) - 1}\x00"

    text = _FENCED_CODE_RE.sub(_protect_fence, text)
    text = _INLINE_CODE_RE.sub(lambda m: "`" + m.group(1).replace("\\", "/") + "`", text)
    for i, fence in enumerate(fences):
        text = text.replace(f"\x00FENCE{i}\x00", fence)
    return text


def clean_body(body: str, params: dict[str, Any]) -> str:
    text = body

    if NORMALIZE_WINDOWS_PATHS:
        text = _normalize_windows_paths(text)

    # Convert Hugo table shortcodes (wrapping raw HTML tables) to plain text.
    def _table_sub(m: re.Match) -> str:
        return "\n" + _html_table_to_text(m.group(1)) + "\n"

    text = re.sub(
        r"\{\{<table[^>]*>\}\}(.*?)\{\{</table>\}\}", _table_sub, text, flags=re.DOTALL
    )

    # Drop purely visual paired shortcodes entirely (including their body).
    for name in _DROP_PAIRED:
        text = re.sub(
            r"\{\{<\s*" + name + r"\b.*?\}\}.*?\{\{</\s*" + name + r"\s*>\}\}",
            "",
            text,
            flags=re.DOTALL,
        )

    # Drop purely visual self-closing / single-tag shortcodes.
    for name in _DROP_SELF:
        text = re.sub(r"\{\{[<%]\s*" + name + r"\b.*?[%>]\}\}", "", text, flags=re.DOTALL)

    # Resolve {{<param "key">}} against frontmatter params.
    def _param_sub(m: re.Match) -> str:
        key = m.group(1)
        value = params.get(key, "") if isinstance(params, dict) else ""
        return str(value) if value is not None else ""

    text = re.sub(r'\{\{<param\s+"([^"]+)"\s*>\}\}', _param_sub, text)

    # relref/ref -> keep the referenced path as plain text.
    text = re.sub(r'\{\{<\s*relref\s+"([^"]+)"\s*>\}\}', r"\1", text)
    text = re.sub(r'\{\{<\s*ref\s+"([^"]+)"\s*>\}\}', r"\1", text)

    # Unwrap remaining structural shortcodes, keep inner text.
    for name in _UNWRAP:
        text = re.sub(r"\{\{[<%]/?\s*" + name + r"\b[^%>]*[%>]\}\}", "", text)

    # Anything else shortcode-shaped: drop the tag, keep surrounding text.
    text = re.sub(r"\{\{[<%].*?[%>]\}\}", "", text, flags=re.DOTALL)

    # Drop leftover raw HTML annotation overlays and any other tags.
    text = re.sub(r"(?is)<map\b.*?</map\s*>", "", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)

    # Collapse excess blank lines / trailing whitespace.
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# --------------------------------------------------------------------------
# Section chunking
# --------------------------------------------------------------------------


@dataclass
class Section:
    heading: str | None
    text: str
    level: int | None = None
    # The enclosing ## heading, only set for ### sections -- carried into the
    # chunk breadcrumb so a ### subsection under an unrelated-sounding parent
    # (e.g. "Options" under "Conversion Modes") doesn't lose that context.
    parent_heading: str | None = None


def split_sections(clean_text: str) -> list[Section]:
    matches = list(HEADING_RE.finditer(clean_text))
    sections: list[Section] = []

    intro_end = matches[0].start() if matches else len(clean_text)
    intro = clean_text[:intro_end].strip()
    if intro:
        sections.append(Section(heading=None, text=intro))

    current_h2: str | None = None
    for i, m in enumerate(matches):
        level = len(m.group(1))
        heading = m.group(2).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(clean_text)
        body = clean_text[start:end].strip()
        parent_heading = current_h2 if level == 3 else None
        sections.append(Section(heading=heading, text=body, level=level, parent_heading=parent_heading))
        if level == 2:
            current_h2 = heading

    return sections


def _merge_short_sections(sections: list[Section]) -> list[Section]:
    # A section shorter than CHUNK_MIN_CHARS carries too little text to
    # stand alone (e.g. a heading followed by one short sentence before its
    # own subsections) -- glue it onto the next sibling at the same heading
    # level instead of silently dropping its content, falling back to the
    # previous section when it's the last one on the page.
    merged: list[Section] = []
    i = 0
    n = len(sections)
    while i < n:
        section = sections[i]
        if len(section.text) < CHUNK_MIN_CHARS and n > 1:
            nxt = sections[i + 1] if i + 1 < n else None
            if nxt is not None and nxt.level == section.level:
                combined = f"{section.text}\n\n{nxt.text}" if section.text else nxt.text
                merged.append(
                    Section(heading=nxt.heading, text=combined, level=nxt.level, parent_heading=nxt.parent_heading)
                )
                i += 2
                continue
            if merged:
                prev = merged[-1]
                merged[-1] = Section(
                    heading=prev.heading,
                    text=f"{prev.text}\n\n{section.text}" if section.text else prev.text,
                    level=prev.level,
                    parent_heading=prev.parent_heading,
                )
                i += 1
                continue
        merged.append(section)
        i += 1
    return merged


def _pack(pieces: list[str], sep: str, max_chars: int, overlap: int = 0) -> list[str]:
    chunks: list[str] = []
    current = ""
    for piece in pieces:
        candidate = f"{current}{sep}{piece}" if current else piece
        if len(candidate) > max_chars and current:
            chunks.append(current.strip())
            # Carry the flushed chunk's tail into the next one so a piece
            # split across the boundary still has surrounding context on
            # both sides (see CHUNK_OVERLAP_CHARS in ingest.py).
            tail = current[-overlap:] if overlap else ""
            current = f"{tail}{sep}{piece}" if tail else piece
        else:
            current = candidate
    if current.strip():
        chunks.append(current.strip())
    return chunks


def _hard_wrap(text: str, max_chars: int, overlap: int = 0) -> list[str]:
    step = max_chars - overlap if overlap else max_chars
    return [text[i : i + max_chars] for i in range(0, len(text), step)]


CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)

# Identifier-like tokens for the exact-match dictionary: CLI flags
# (--foo-bar), and snake_case/kebab-case/dotted names
# with at least one separator (config keys, method paths like Foo.bar) --
# plain English words never match since they lack a '.', '_', or '-'.
IDENTIFIER_RE = re.compile(r"--[A-Za-z][\w-]*|\b[A-Za-z_][A-Za-z0-9_]*(?:[._-][A-Za-z0-9_]+)+\b")


def extract_identifiers(text: str) -> list[str]:
    return sorted({m.group(0).lower() for m in IDENTIFIER_RE.finditer(text)})


def _pack_prose(text: str, max_chars: int, overlap: int = 0) -> list[str]:
    result: list[str] = []
    for paragraph in _pack(re.split(r"\n{2,}", text), "\n\n", max_chars, overlap):
        if len(paragraph) <= max_chars:
            result.append(paragraph)
            continue
        # Single oversized paragraph (e.g. a huge converted table): fall
        # back to line-level packing, then hard-wrap any still-long line.
        for line_chunk in _pack(paragraph.split("\n"), "\n", max_chars, overlap):
            if len(line_chunk) <= max_chars:
                result.append(line_chunk)
            else:
                result.extend(_hard_wrap(line_chunk, max_chars, overlap))
    return result


def _split_long_text(text: str, max_chars: int, overlap: int = 0) -> list[str]:
    if len(text) <= max_chars:
        return [text]

    # Fenced code blocks (config snippets, method examples) must survive as
    # a single unit even past max_chars -- splitting one mid-block makes
    # both search and the generated answer incomplete for "what params does
    # X take" / "example config for Y" style queries. Pull them out first so
    # the paragraph-based splitter below (which would otherwise treat blank
    # lines *inside* a code block as paragraph boundaries) never sees them;
    # everything else is packed by paragraph, then line, then hard-wrapped
    # as before. _pack never slices an individual oversized piece -- it just
    # becomes its own chunk -- so a code block always comes out intact.
    units: list[str] = []
    pos = 0
    for m in CODE_FENCE_RE.finditer(text):
        prose_before = text[pos : m.start()]
        if prose_before.strip():
            units.extend(_pack_prose(prose_before, max_chars, overlap))
        code_block = m.group().strip()
        if code_block:
            units.append(code_block)
        pos = m.end()
    trailing = text[pos:]
    if trailing.strip():
        units.extend(_pack_prose(trailing, max_chars, overlap))

    if not units:
        return [text]
    return _pack(units, "\n\n", max_chars, overlap)


@dataclass
class Chunk:
    text: str
    heading: str | None
    chunk_index: int
    heading_level: int | None = None
    section_path: list[str] | None = None
    chunk_type: str = "prose"


def build_chunks(page: Page, clean_text: str, section_title: str | None = None) -> list[Chunk]:
    title = page.meta.get("title") or page.rel_path
    description = page.meta.get("description")
    sections = _merge_short_sections(split_sections(clean_text))

    raw_chunks: list[tuple[Section, str]] = []
    for section in sections:
        if len(section.text) < CHUNK_MIN_CHARS:
            continue
        for piece in _split_long_text(section.text, CHUNK_MAX_CHARS, CHUNK_OVERLAP_CHARS):
            if len(piece) < CHUNK_MIN_CHARS:
                continue
            raw_chunks.append((section, piece))

    chunks: list[Chunk] = []
    idx = 0
    if description:
        # A standalone chunk for the page's own description, instead of
        # gluing it onto every per-heading chunk below like the rest of
        # this function used to: that made every chunk on the page open
        # with the same boilerplate sentence, so a query that happens to
        # match the description (e.g. "how do I install and launch X" when
        # description is "How to install and launch X") scored every chunk
        # on the page near-identically instead of discriminating between
        # them -- see RESULTS.md's 2026-07-14 investigation.
        breadcrumb = f"{section_title} — {title}" if section_title and section_title != title else str(title)
        chunks.append(Chunk(text=f"{breadcrumb}\n\n{description}", heading=None, chunk_index=idx))
        idx += 1

    for section, piece in raw_chunks:
        heading = section.heading
        heading_path = [h for h in (section.parent_heading, heading) if h]
        breadcrumb = f"{title} > {' > '.join(heading_path)}" if heading_path else str(title)
        # Pages under a section (e.g. ue/getting-started/installation) often
        # have a generic title ("Installation") that never mentions the
        # section's own name ("Unified Editor") -- without it, chunks whose
        # body text also doesn't repeat the section name (e.g. procedural
        # steps like "Create a download folder...") drift semantically away
        # from queries like "how to install <section>". Skip this for the
        # section's own index page, where section_title == title already.
        if section_title and section_title != title:
            breadcrumb = f"{section_title} — {breadcrumb}"
        embed_text = f"{breadcrumb}\n\n{piece}"
        stripped = piece.strip()
        chunk_type = "code" if stripped.startswith("```") and stripped.endswith("```") else "prose"
        chunks.append(
            Chunk(
                text=embed_text,
                heading=heading,
                chunk_index=idx,
                heading_level=section.level,
                section_path=heading_path or None,
                chunk_type=chunk_type,
            )
        )
        idx += 1
    return chunks


# --------------------------------------------------------------------------
# Embedding + Qdrant
# --------------------------------------------------------------------------


def point_id(rel_path: str, chunk_index: int) -> str:
    return str(uuid.uuid5(POINT_NAMESPACE, f"{rel_path}::{chunk_index}"))


def summary_point_id(rel_path: str) -> str:
    # One summary point per page (not per chunk), same namespace/uuid5
    # scheme as point_id -- deterministic across runs.
    return str(uuid.uuid5(POINT_NAMESPACE, f"{rel_path}::summary"))


def generate_summary(reasoning_client: ReasoningClient, text: str) -> str:
    """3-5 sentence page summary for the summary index. Best-effort: any
    failure (timeout, gateway error) is logged as a
    warning and returns "" so the caller skips upserting a stale/missing
    summary rather than aborting the whole file's ingest over a non-essential
    step."""
    try:
        result = reasoning_client.generate(AI_GATEWAY_REASONING_MODEL, SUMMARY_SYSTEM_PROMPT, text)
    except Exception as exc:
        logger.warning("summary generation failed: %s", exc)
        return ""
    return result.text.strip()


def iter_content_files(root: Path):
    for path in sorted(root.rglob("*.md")):
        if path.is_file():
            yield path


def build_points(
    page: Page,
    clean_text: str,
    embed_fn: Callable[[str], list[float]],
    section_title: str | None = None,
) -> list[PointStruct]:
    chunks = build_chunks(page, clean_text, section_title)
    if not chunks:
        return []

    title = page.meta.get("title") or page.rel_path
    date = page.meta.get("date")
    lastmod = page.meta.get("lastmod") or date
    tags = derive_tags(page.rel_path, page.meta)

    points: list[PointStruct] = []
    for chunk in chunks:
        vector = embed_fn(chunk.text)
        sparse_vec = compute_sparse_vector(chunk.text)
        payload = {
            "text": chunk.text,
            "source_path": page.rel_path,
            "title": str(title),
            "heading": chunk.heading,
            "heading_level": chunk.heading_level,
            "section_path": chunk.section_path,
            "chunk_type": chunk.chunk_type,
            "date": str(date) if date else None,
            "lastmod": str(lastmod) if lastmod else None,
            "weight": page.meta.get("weight"),
            "description": page.meta.get("description"),
            "tags": tags,
            "chunk_index": chunk.chunk_index,
            # Always present (empty list if none/not a code chunk) so the
            # payload index has a stable schema.
            "identifiers": extract_identifiers(chunk.text) if chunk.chunk_type == "code" else [],
        }
        points.append(
            PointStruct(
                id=point_id(page.rel_path, chunk.chunk_index),
                vector={"dense": vector, "sparse": sparse_vec},
                payload=payload,
            )
        )
    return points


# --------------------------------------------------------------------------
# Change tracking (manifest of per-file content hashes)
# --------------------------------------------------------------------------


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_manifest(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}


def save_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True))


def delete_points_for_path(qdrant: QdrantClient, rel_path: str) -> None:
    qdrant.delete(
        collection_name=QDRANT_COLLECTION,
        points_selector=Filter(
            must=[FieldCondition(key="source_path", match=MatchValue(value=rel_path))]
        ),
    )


def delete_summary_for_path(qdrant: QdrantClient, rel_path: str) -> None:
    # Guarded by collection_exists -- the summary collection may not exist
    # yet (force rebuild, or a fresh volume before the first non-empty page
    # is ingested), unlike the main collection which this function's sibling
    # above assumes is already there.
    if not qdrant.collection_exists(QDRANT_SUMMARY_COLLECTION):
        return
    qdrant.delete(
        collection_name=QDRANT_SUMMARY_COLLECTION,
        points_selector=Filter(
            must=[FieldCondition(key="source_path", match=MatchValue(value=rel_path))]
        ),
    )


# --------------------------------------------------------------------------
# Lock (prevents concurrent ingest runs against the same STATE_DIR)
# --------------------------------------------------------------------------


def acquire_lock() -> None:
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        held_since = LOCK_PATH.read_text().strip() if LOCK_PATH.is_file() else "unknown"
        logger.error(
            "another ingest run is already in progress (%s) -- refusing to start a second one. "
            "If that run crashed without cleaning up, remove %s manually and retry.",
            held_since,
            LOCK_PATH,
        )
        raise SystemExit(1)
    with os.fdopen(fd, "w") as f:
        f.write(f"pid={os.getpid()} started={datetime.now(timezone.utc).isoformat()}\n")


def release_lock() -> None:
    LOCK_PATH.unlink(missing_ok=True)


def main() -> int:
    acquire_lock()
    try:
        return _run()
    finally:
        release_lock()


def _run() -> int:
    with tracer.start_as_current_span("ingest_run") as run_span:
        return _run_traced(run_span)


def _run_traced(run_span) -> int:
    if not CONTENT_DIR.is_dir():
        logger.error("content dir not found: %s", CONTENT_DIR)
        return 1

    force = FORCE_INGEST
    qdrant = QdrantClient(url=QDRANT_URL)

    # The manifest is keyed by file path, not by collection -- switching
    # QDRANT_COLLECTION without wiping/renaming the manifest would otherwise
    # make an incremental run think a brand-new (or emptied) collection is
    # already fully indexed and write nothing to it. Force a full rebuild
    # whenever the target collection is missing or has no points, regardless
    # of what the manifest says.
    if not force and not qdrant.collection_exists(QDRANT_COLLECTION):
        logger.info("collection '%s' does not exist yet -- forcing a full rebuild", QDRANT_COLLECTION)
        force = True
    elif not force and qdrant.count(QDRANT_COLLECTION, exact=True).count == 0:
        logger.info("collection '%s' exists but is empty -- forcing a full rebuild", QDRANT_COLLECTION)
        force = True

    run_span.set_attribute("ingest.force", force)

    old_manifest = {} if force else load_manifest(MANIFEST_PATH)
    logger.info(
        "FORCE_INGEST=true -- full rebuild"
        if force
        else f"incremental run against {len(old_manifest)} previously indexed files"
    )

    files = list(iter_content_files(CONTENT_DIR))
    logger.info("found %d markdown files under %s", len(files), CONTENT_DIR)
    run_span.set_attribute("ingest.total_files", len(files))
    files_by_rel = {str(p.relative_to(CONTENT_DIR)): p for p in files}
    current_hashes = {rel: file_hash(p) for rel, p in files_by_rel.items()}

    # Top-level content directory (e.g. "ue") -> that section's own title
    # (e.g. "Unified Editor"), read from its _index.md. Threaded into
    # build_points so sub-pages with generic titles still carry their
    # section's identity in the embedded text -- see build_chunks.
    section_titles: dict[str, str] = {}
    for rel_path, path in files_by_rel.items():
        parts = Path(rel_path).parts
        if len(parts) == 2 and parts[1] == "_index.md":
            meta, _ = split_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
            title = meta.get("title")
            if title:
                section_titles[parts[0]] = str(title)

    if force:
        changed_paths = set(current_hashes)
        removed_paths: set[str] = set()
    else:
        changed_paths = {
            rel
            for rel, h in current_hashes.items()
            if rel not in old_manifest or old_manifest[rel].get("hash") != h
        }
        removed_paths = set(old_manifest) - set(current_hashes)

    run_span.set_attribute("ingest.changed_files", len(changed_paths))
    run_span.set_attribute("ingest.removed_files", len(removed_paths))

    if not force and not changed_paths and not removed_paths:
        logger.info("nothing changed since last run -- done")
        return 0

    logger.info("%d new/changed file(s), %d removed file(s)", len(changed_paths), len(removed_paths))

    manifest: dict[str, Any] = {} if force else dict(old_manifest)
    for rel_path in sorted(removed_paths):
        logger.info("  removing  %s", rel_path)
        delete_points_for_path(qdrant, rel_path)
        delete_summary_for_path(qdrant, rel_path)
        manifest.pop(rel_path, None)

    embedding_client = EmbeddingClient(
        AI_GATEWAY_URL,
        AI_GATEWAY_EMBEDDINGS_MODEL,
        EMBEDDING_TIMEOUT,
        AI_GATEWAY_AUTH_KEY,
        AI_GATEWAY_AUTH_HEADER,
        AI_GATEWAY_AUTH_VALUE_TEMPLATE,
    )
    embed_fn = embedding_client.embed_query
    reasoning_client = ReasoningClient(
        AI_GATEWAY_URL,
        SUMMARY_TIMEOUT,
        AI_GATEWAY_AUTH_KEY,
        AI_GATEWAY_AUTH_HEADER,
        AI_GATEWAY_AUTH_VALUE_TEMPLATE,
    )
    files_with_content = 0
    files_skipped_empty = 0
    files_failed = 0
    total_chunks = 0
    total_upserted = 0
    # False only in force mode, until the first non-empty file's points tell
    # us vector_size -- recreate_collection happens then, not after every
    # file has already been walked/embedded. pending_points is a rolling
    # buffer flushed every UPSERT_BATCH_SIZE (and the manifest is saved after
    # every file), instead of the whole run's points sitting in memory until
    # one write at the very end -- a crash partway through a large run (e.g.
    # the AI gateway times out on file 150/263) now keeps whatever was embedded and
    # upserted so far, rather than losing the entire run.
    collection_ready = not force
    # Tracked separately from collection_ready: an existing deployment
    # upgrading to the summary index has a main
    # collection already but no summary collection yet, so this can't assume
    # "not force" means "already exists" the way collection_ready does.
    summary_collection_ready = not force and qdrant.collection_exists(QDRANT_SUMMARY_COLLECTION)
    pending_points: list[PointStruct] = []

    sorted_changed_paths = sorted(changed_paths)
    total_changed = len(sorted_changed_paths)
    for i, rel_path in enumerate(sorted_changed_paths, start=1):
        progress = f"[{i}/{total_changed}, {round(100 * i / total_changed)}%]"
        with tracer.start_as_current_span("ingest_file") as file_span:
            file_span.set_attribute("ingest.rel_path", rel_path)
            if not force:
                delete_points_for_path(qdrant, rel_path)
                delete_summary_for_path(qdrant, rel_path)

            page = load_page(files_by_rel[rel_path])
            if page.meta.get("draft") is True:
                points: list[PointStruct] = []
            else:
                params = page.meta.get("params") or {}
                clean_text = clean_body(page.body, params)
                parts = Path(rel_path).parts
                section_title = section_titles.get(parts[0]) if len(parts) > 1 else None
                try:
                    points = build_points(page, clean_text, embed_fn, section_title)
                except httpx.HTTPStatusError as exc:
                    # A gateway error on one file (e.g. a transient/model-
                    # specific 400) shouldn't abort every other file's ingest --
                    # skip it, and leave it out of the manifest so the next run
                    # retries it instead of treating it as successfully ingested.
                    files_failed += 1
                    logger.error("  %s FAILED     %s: %s", progress, rel_path, exc)
                    continue
            file_span.set_attribute("ingest.chunk_count", len(points))

            # vocab_terms: title/description/tags + every chunk's heading,
            # for typo-correction's vocabulary (see VOCAB_PATH above). Kept
            # per-manifest-entry (not recomputed globally) so an unchanged
            # page's terms survive an incremental run without re-reading it.
            vocab_terms = sorted(
                extract_terms(
                    page.meta.get("title"),
                    page.meta.get("description"),
                    *derive_tags(rel_path, page.meta),
                    *(p.payload.get("heading") for p in points if p.payload),
                )
            )
            manifest[rel_path] = {
                "hash": current_hashes[rel_path],
                "chunk_count": len(points),
                "vocab_terms": vocab_terms,
            }

            if not points:
                files_skipped_empty += 1
                logger.info("  %s skipped   (empty)  %s", progress, rel_path)
                save_manifest(MANIFEST_PATH, manifest)
                continue

            files_with_content += 1
            total_chunks += len(points)

            if not collection_ready:
                vector_size = len(cast(dict, points[0].vector)["dense"])
                logger.info("recreating collection '%s' (size=%d)", QDRANT_COLLECTION, vector_size)
                # recreate_collection is deprecated as of qdrant-client 1.10 --
                # explicit delete (if present) + create is the replacement.
                if qdrant.collection_exists(QDRANT_COLLECTION):
                    qdrant.delete_collection(QDRANT_COLLECTION)
                qdrant.create_collection(
                    collection_name=QDRANT_COLLECTION,
                    # Named vectors: "dense" is the existing embedding search
                    # (float16 halves on-disk/in-memory size vs. the float32
                    # default, with no measurable precision loss for cosine
                    # search); "sparse" is the term-frequency lexical vector
                    # from compute_sparse_vector, for hybrid search.
                    # Modifier.IDF makes Qdrant
                    # compute and apply IDF server-side from the raw counts
                    # ingest sends -- ingest itself doesn't do any manual IDF
                    # weighting. Retrieval-side fusion (RRF) between dense and
                    # sparse results is out of scope here (mcp_server's job);
                    # this only makes the sparse vectors exist to fuse over.
                    vectors_config={
                        "dense": VectorParams(size=vector_size, distance=Distance.COSINE, datatype=Datatype.FLOAT16)
                    },
                    sparse_vectors_config={
                        "sparse": SparseVectorParams(index=SparseIndexParams(), modifier=Modifier.IDF)
                    },
                )
                # Created before points are upserted -- payload indexes created
                # after HNSW is built don't get the extra filterable-vector-search
                # links. Keyword (exact-match) indexes for the fields
                # mcp-server's retrieve() can filter on (see libs/retrieval.py).
                for field_name in ("title", "description", "source_path", "tags", "identifiers"):
                    qdrant.create_payload_index(
                        collection_name=QDRANT_COLLECTION, field_name=field_name, field_schema=PayloadSchemaType.KEYWORD
                    )
                collection_ready = True

            # Page-level summary -- generated
            # from the same cleaned body text the chunks came from, so it
            # covers the whole page. Best-effort: generate_summary() returns
            # "" on failure (logged there), in which case this page is left
            # without a summary until a future run succeeds -- the stale
            # summary was already deleted above, so this doesn't risk serving
            # an outdated one.
            summary_text = generate_summary(reasoning_client, clean_text)
            if summary_text:
                summary_vector = embed_fn(summary_text)
                if not summary_collection_ready:
                    # Tracked independently from collection_ready (main
                    # collection) -- an existing deployment upgrading to this
                    # feature has a main collection already but no summary
                    # collection yet, so this can't just piggyback on the
                    # main collection's own recreate-on-first-file trigger.
                    logger.info(
                        "recreating collection '%s' (size=%d)", QDRANT_SUMMARY_COLLECTION, len(summary_vector)
                    )
                    if qdrant.collection_exists(QDRANT_SUMMARY_COLLECTION):
                        qdrant.delete_collection(QDRANT_SUMMARY_COLLECTION)
                    qdrant.create_collection(
                        collection_name=QDRANT_SUMMARY_COLLECTION,
                        vectors_config=VectorParams(
                            size=len(summary_vector), distance=Distance.COSINE, datatype=Datatype.FLOAT16
                        ),
                    )
                    for field_name in ("title", "description", "source_path", "tags"):
                        qdrant.create_payload_index(
                            collection_name=QDRANT_SUMMARY_COLLECTION,
                            field_name=field_name,
                            field_schema=PayloadSchemaType.KEYWORD,
                        )
                    summary_collection_ready = True
                qdrant.upsert(
                    collection_name=QDRANT_SUMMARY_COLLECTION,
                    points=[
                        PointStruct(
                            id=summary_point_id(rel_path),
                            vector=summary_vector,
                            payload={
                                "title": str(page.meta.get("title") or rel_path),
                                "description": page.meta.get("description"),
                                "source_path": rel_path,
                                "tags": derive_tags(rel_path, page.meta),
                                "summary_text": summary_text,
                            },
                        )
                    ],
                )
            else:
                logger.warning("  %s no summary   %s", progress, rel_path)

            pending_points.extend(points)
            # Embedding (above) already happened; this chunk count is buffered in
            # memory, not yet written to Qdrant -- that only happens once
            # pending_points below reaches UPSERT_BATCH_SIZE, which usually spans
            # several files (most pages produce far fewer than
            # UPSERT_BATCH_SIZE chunks each).
            logger.info(
                "  %s embedded  %3d chunks  %s  (buffered %d/%d pending Qdrant write)",
                progress,
                len(points),
                rel_path,
                len(pending_points),
                UPSERT_BATCH_SIZE,
            )
            while len(pending_points) >= UPSERT_BATCH_SIZE:
                batch, pending_points = pending_points[:UPSERT_BATCH_SIZE], pending_points[UPSERT_BATCH_SIZE:]
                with tracer.start_as_current_span("upsert_batch") as batch_span:
                    batch_span.set_attribute("ingest.batch_size", len(batch))
                    qdrant.upsert(collection_name=QDRANT_COLLECTION, points=batch)
                total_upserted += len(batch)
                logger.info("  >>> upserted %d chunks to Qdrant (%d total so far)", len(batch), total_upserted)

            save_manifest(MANIFEST_PATH, manifest)

    embedding_client.close()
    reasoning_client.close()

    if pending_points:
        remaining = len(pending_points)
        with tracer.start_as_current_span("upsert_batch") as batch_span:
            batch_span.set_attribute("ingest.batch_size", remaining)
            qdrant.upsert(collection_name=QDRANT_COLLECTION, points=pending_points)
        total_upserted += remaining
        logger.info("  >>> upserted final %d chunks to Qdrant (%d total so far)", remaining, total_upserted)
    elif force and not collection_ready:
        logger.info("no content to ingest (no non-empty pages found); leaving collection untouched")

    save_manifest(MANIFEST_PATH, manifest)

    vocab: set[str] = set()
    for entry in manifest.values():
        vocab.update(entry.get("vocab_terms") or [])
    save_vocab(VOCAB_PATH, vocab)
    logger.info("wrote vocab.json with %d terms", len(vocab))

    run_span.set_attribute("ingest.files_with_content", files_with_content)
    run_span.set_attribute("ingest.total_chunks", total_chunks)
    run_span.set_attribute("ingest.files_failed", files_failed)
    logger.info(
        "%d pages changed+ingested, %d changed pages now empty, %d removed, %d failed, %d chunks embedded this run",
        files_with_content,
        files_skipped_empty,
        len(removed_paths),
        files_failed,
        total_chunks,
    )
    if files_failed:
        logger.warning("%d file(s) failed to ingest and will be retried next run -- see FAILED lines above", files_failed)
    logger.info("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
