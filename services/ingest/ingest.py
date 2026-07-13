"""Ingest Hugo content from CONTENT_DIR into Qdrant.

Walks the content tree, parses Hugo front matter, strips Hugo shortcodes /
raw HTML noise, chunks each page by section (## / ### headings), embeds
each chunk with an Ollama embedding model, and upserts everything into a
Qdrant collection.

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

import functools
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
from typing import Any, Callable

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
    PayloadSchemaType,
    PointStruct,
    VectorParams,
)

from _common.config import load_yaml_config
from _common.ollama import build_client, embed_query

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
OLLAMA_BASE_URL = env.str("OLLAMA_BASE_URL")
MODEL_EMBED = env.str("MODEL_EMBED")
FORCE_INGEST = env.bool("FORCE_INGEST")

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


class IngestConfig(BaseModel):
    paths: _PathsConfig
    chunking: _ChunkingConfig
    upsert_batch_size: int = Field(gt=0)
    ollama_timeout: float = Field(gt=0)


_config = load_yaml_config(Path("config.yml"), IngestConfig)

CONTENT_DIR = Path(_config.paths.content_dir)
STATE_DIR = Path(_config.paths.state_dir)
CHUNK_MAX_CHARS = _config.chunking.max_chars
CHUNK_MIN_CHARS = _config.chunking.min_chars
# Clamp defensively -- a misconfigured overlap >= max_chars would make _pack's
# "current" never shrink, growing every chunk to max_chars and looping.
CHUNK_OVERLAP_CHARS = min(_config.chunking.overlap_chars, CHUNK_MAX_CHARS - 1)
UPSERT_BATCH_SIZE = _config.upsert_batch_size
OLLAMA_TIMEOUT = _config.ollama_timeout

MANIFEST_PATH = STATE_DIR / "manifest.json"

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


def clean_body(body: str, params: dict[str, Any]) -> str:
    text = body

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


def split_sections(clean_text: str) -> list[Section]:
    matches = list(HEADING_RE.finditer(clean_text))
    sections: list[Section] = []

    intro_end = matches[0].start() if matches else len(clean_text)
    intro = clean_text[:intro_end].strip()
    if intro:
        sections.append(Section(heading=None, text=intro))

    for i, m in enumerate(matches):
        heading = m.group(2).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(clean_text)
        body = clean_text[start:end].strip()
        sections.append(Section(heading=heading, text=body))

    return sections


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


def _split_long_text(text: str, max_chars: int, overlap: int = 0) -> list[str]:
    if len(text) <= max_chars:
        return [text]

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


@dataclass
class Chunk:
    text: str
    heading: str | None
    chunk_index: int


def build_chunks(page: Page, clean_text: str, section_title: str | None = None) -> list[Chunk]:
    title = page.meta.get("title") or page.rel_path
    description = page.meta.get("description")
    sections = split_sections(clean_text)

    raw_chunks: list[tuple[str | None, str]] = []
    for section in sections:
        if len(section.text) < CHUNK_MIN_CHARS:
            continue
        for piece in _split_long_text(section.text, CHUNK_MAX_CHARS, CHUNK_OVERLAP_CHARS):
            if len(piece) < CHUNK_MIN_CHARS:
                continue
            raw_chunks.append((section.heading, piece))

    chunks: list[Chunk] = []
    for idx, (heading, piece) in enumerate(raw_chunks):
        breadcrumb = f"{title} > {heading}" if heading else str(title)
        # Pages under a section (e.g. ue/getting-started/installation) often
        # have a generic title ("Installation") that never mentions the
        # section's own name ("Unified Editor") -- without it, chunks whose
        # body text also doesn't repeat the section name (e.g. procedural
        # steps like "Create a download folder...") drift semantically away
        # from queries like "how to install <section>". Skip this for the
        # section's own index page, where section_title == title already.
        if section_title and section_title != title:
            breadcrumb = f"{section_title} — {breadcrumb}"
        header = breadcrumb if not description else f"{breadcrumb}\n{description}"
        embed_text = f"{header}\n\n{piece}"
        chunks.append(Chunk(text=embed_text, heading=heading, chunk_index=idx))
    return chunks


# --------------------------------------------------------------------------
# Embedding + Qdrant
# --------------------------------------------------------------------------


def point_id(rel_path: str, chunk_index: int) -> str:
    return str(uuid.uuid5(POINT_NAMESPACE, f"{rel_path}::{chunk_index}"))


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
        payload = {
            "text": chunk.text,
            "source_path": page.rel_path,
            "title": str(title),
            "heading": chunk.heading,
            "date": str(date) if date else None,
            "lastmod": str(lastmod) if lastmod else None,
            "weight": page.meta.get("weight"),
            "description": page.meta.get("description"),
            "tags": tags,
            "chunk_index": chunk.chunk_index,
        }
        points.append(
            PointStruct(
                id=point_id(page.rel_path, chunk.chunk_index), vector=vector, payload=payload
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


# --------------------------------------------------------------------------
# Lock (prevents concurrent ingest runs against the same STATE_DIR)
# --------------------------------------------------------------------------


def acquire_lock() -> None:
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        held_since = LOCK_PATH.read_text().strip() if LOCK_PATH.is_file() else "unknown"
        print(
            f"another ingest run is already in progress ({held_since}) -- "
            f"refusing to start a second one. If that run crashed without "
            f"cleaning up, remove {LOCK_PATH} manually and retry.",
            file=sys.stderr,
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
    if not CONTENT_DIR.is_dir():
        print(f"content dir not found: {CONTENT_DIR}", file=sys.stderr)
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
        print(f"collection '{QDRANT_COLLECTION}' does not exist yet -- forcing a full rebuild")
        force = True
    elif not force and qdrant.count(QDRANT_COLLECTION, exact=True).count == 0:
        print(f"collection '{QDRANT_COLLECTION}' exists but is empty -- forcing a full rebuild")
        force = True

    old_manifest = {} if force else load_manifest(MANIFEST_PATH)
    print("FORCE_INGEST=true -- full rebuild" if force else f"incremental run against {len(old_manifest)} previously indexed files")

    files = list(iter_content_files(CONTENT_DIR))
    print(f"found {len(files)} markdown files under {CONTENT_DIR}")
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

    if not force and not changed_paths and not removed_paths:
        print("nothing changed since last run -- done")
        return 0

    print(f"{len(changed_paths)} new/changed file(s), {len(removed_paths)} removed file(s)")

    manifest: dict[str, Any] = {} if force else dict(old_manifest)
    for rel_path in sorted(removed_paths):
        print(f"  removing  {rel_path}")
        delete_points_for_path(qdrant, rel_path)
        manifest.pop(rel_path, None)

    ollama_client = build_client(OLLAMA_BASE_URL, OLLAMA_TIMEOUT)
    embed_fn = functools.partial(embed_query, ollama_client, MODEL_EMBED)
    files_with_content = 0
    files_skipped_empty = 0
    total_chunks = 0
    total_upserted = 0
    # False only in force mode, until the first non-empty file's points tell
    # us vector_size -- recreate_collection happens then, not after every
    # file has already been walked/embedded. pending_points is a rolling
    # buffer flushed every UPSERT_BATCH_SIZE (and the manifest is saved after
    # every file), instead of the whole run's points sitting in memory until
    # one write at the very end -- a crash partway through a large run (e.g.
    # Ollama times out on file 150/263) now keeps whatever was embedded and
    # upserted so far, rather than losing the entire run.
    collection_ready = not force
    pending_points: list[PointStruct] = []

    sorted_changed_paths = sorted(changed_paths)
    total_changed = len(sorted_changed_paths)
    for i, rel_path in enumerate(sorted_changed_paths, start=1):
        progress = f"[{i}/{total_changed}, {round(100 * i / total_changed)}%]"
        if not force:
            delete_points_for_path(qdrant, rel_path)

        page = load_page(files_by_rel[rel_path])
        if page.meta.get("draft") is True:
            points: list[PointStruct] = []
        else:
            params = page.meta.get("params") or {}
            clean_text = clean_body(page.body, params)
            parts = Path(rel_path).parts
            section_title = section_titles.get(parts[0]) if len(parts) > 1 else None
            points = build_points(page, clean_text, embed_fn, section_title)

        manifest[rel_path] = {"hash": current_hashes[rel_path], "chunk_count": len(points)}

        if not points:
            files_skipped_empty += 1
            print(f"  {progress} skipped   (empty)  {rel_path}")
            save_manifest(MANIFEST_PATH, manifest)
            continue

        files_with_content += 1
        total_chunks += len(points)

        if not collection_ready:
            vector_size = len(points[0].vector)
            print(f"recreating collection '{QDRANT_COLLECTION}' (size={vector_size})")
            # recreate_collection is deprecated as of qdrant-client 1.10 --
            # explicit delete (if present) + create is the replacement.
            if qdrant.collection_exists(QDRANT_COLLECTION):
                qdrant.delete_collection(QDRANT_COLLECTION)
            qdrant.create_collection(
                collection_name=QDRANT_COLLECTION,
                # float16 halves on-disk/in-memory vector size vs. the
                # float32 default, with no measurable precision loss for
                # cosine search.
                vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE, datatype=Datatype.FLOAT16),
            )
            # Created before points are upserted -- payload indexes created
            # after HNSW is built don't get the extra filterable-vector-search
            # links. Keyword (exact-match) indexes for the fields
            # mcp-server's retrieve() can filter on (see libs/retrieval.py).
            for field_name in ("title", "description", "source_path"):
                qdrant.create_payload_index(
                    collection_name=QDRANT_COLLECTION, field_name=field_name, field_schema=PayloadSchemaType.KEYWORD
                )
            collection_ready = True

        pending_points.extend(points)
        # Embedding (above) already happened; this chunk count is buffered in
        # memory, not yet written to Qdrant -- that only happens once
        # pending_points below reaches UPSERT_BATCH_SIZE, which usually spans
        # several files (most pages produce far fewer than
        # UPSERT_BATCH_SIZE chunks each).
        print(
            f"  {progress} embedded  {len(points):3d} chunks  {rel_path}"
            f"  (buffered {len(pending_points)}/{UPSERT_BATCH_SIZE} pending Qdrant write)"
        )
        while len(pending_points) >= UPSERT_BATCH_SIZE:
            batch, pending_points = pending_points[:UPSERT_BATCH_SIZE], pending_points[UPSERT_BATCH_SIZE:]
            qdrant.upsert(collection_name=QDRANT_COLLECTION, points=batch)
            total_upserted += len(batch)
            print(f"  >>> upserted {len(batch)} chunks to Qdrant ({total_upserted} total so far)")

        save_manifest(MANIFEST_PATH, manifest)

    ollama_client.close()

    if pending_points:
        remaining = len(pending_points)
        qdrant.upsert(collection_name=QDRANT_COLLECTION, points=pending_points)
        total_upserted += remaining
        print(f"  >>> upserted final {remaining} chunks to Qdrant ({total_upserted} total so far)")
    elif force and not collection_ready:
        print("no content to ingest (no non-empty pages found); leaving collection untouched")

    save_manifest(MANIFEST_PATH, manifest)

    print(
        f"\n{files_with_content} pages changed+ingested, {files_skipped_empty} changed pages now empty, "
        f"{len(removed_paths)} removed, {total_chunks} chunks embedded this run"
    )
    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
