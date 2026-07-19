"""Fuzzy-corrects query tokens against the shared vocabulary (vocab.json,
built by ingest.py from indexed titles/headings/tags) before sparse
vectorization. Sparse matching is exact-token, so a typo silently drops that
token's contribution entirely -- dense embedding already tolerates minor
typos, so correction only needs to feed the sparse branch, not the text
passed to embed_fn.
"""

from __future__ import annotations

import re

from rapidfuzz import fuzz, process

from _common.vocab import WORD_RE


def correct_tokens(query: str, vocab: set[str], threshold: int = 85) -> tuple[str, int]:
    """Returns (corrected_query, corrections_count). Never invents a
    correction below `threshold` (rapidfuzz 0-100 ratio) -- an untouched
    token is left exactly as typed rather than risk swapping in an unrelated
    but similarly-spelled word."""
    if not vocab:
        return query, 0
    corrected = query
    count = 0
    for token in dict.fromkeys(WORD_RE.findall(query)):  # de-duped, order preserved
        if token.lower() in vocab:
            continue
        match = process.extractOne(token.lower(), vocab, scorer=fuzz.ratio)
        if match and match[1] >= threshold and match[0] != token.lower():
            corrected = re.sub(rf"\b{re.escape(token)}\b", match[0], corrected)
            count += 1
    return corrected, count
