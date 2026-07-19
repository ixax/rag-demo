"""Shared term-frequency sparse-vector hashing for hybrid dense+lexical
search -- used by ingest.py when indexing
chunks and by mcp_server/retrieval.py when embedding a query, so both sides
hash terms into the same fixed index space. Raw term counts only -- no
manual IDF weighting; Qdrant's collection is created with Modifier.IDF, so
IDF is computed and applied server-side from these counts automatically.

Content is English-only technical docs (checked: no per-page
lang/language frontmatter, single content tree) -- so this is deliberately
a plain word tokenizer, not lemmatized/stemmed.
"""

from __future__ import annotations

import re
import zlib

from qdrant_client.models import SparseVector

_SPARSE_VOCAB_SIZE = 2**24
_TOKEN_RE = re.compile(r"[\w][\w\-]*", re.UNICODE)


def sparse_vector(text: str) -> SparseVector:
    counts: dict[int, int] = {}
    for token in _TOKEN_RE.findall(text.lower()):
        idx = zlib.crc32(token.encode("utf-8")) % _SPARSE_VOCAB_SIZE
        counts[idx] = counts.get(idx, 0) + 1
    if not counts:
        return SparseVector(indices=[], values=[])
    indices, values = zip(*sorted(counts.items()))
    return SparseVector(indices=list(indices), values=[float(v) for v in values])
