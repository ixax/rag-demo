"""Query-side lexical expansion using a manually-curated synonym table
(synonyms.yml) -- appends known synonyms of matched terms to the query text
before sparse vectorization, so the exact-token sparse branch also matches
when the user's wording differs from the documentation's own terminology.
Applied only before compute_sparse_vector() -- appending synonym tokens to
the text passed to embed_fn would dilute the dense embedding instead of
helping it.
"""

from __future__ import annotations


def expand_query(query: str, table: dict[str, list[str]]) -> tuple[str, int]:
    if not table:
        return query, 0
    added: list[str] = []
    lowered = query.lower()
    for term, synonyms in table.items():
        if term.lower() in lowered:
            for syn in synonyms:
                if syn.lower() not in lowered:
                    added.append(syn)
    if not added:
        return query, 0
    return f"{query} {' '.join(added)}", len(added)
