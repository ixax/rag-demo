"""Shared vocabulary file (vocab.json) used for query-side typo correction
(services/mcp_server/src/libs/typo_correct.py) -- ingest.py builds it from
indexed titles/headings/tags, mcp-server reads it. Lives in _common because
both services need the exact same word-extraction rule and file format.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

# 3+ word chars (Latin or Cyrillic) or hyphen/underscore -- short tokens
# (1-2 chars) are too ambiguous for fuzzy correction to be useful and would
# just add noise to the vocabulary.
WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁё0-9_-]{3,}")


def extract_terms(*texts: str | None) -> set[str]:
    terms: set[str] = set()
    for text in texts:
        if text:
            terms.update(w.lower() for w in WORD_RE.findall(text))
    return terms


def load_vocab(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    try:
        return set(json.loads(path.read_text()))
    except (json.JSONDecodeError, OSError):
        return set()


def save_vocab(path: Path, terms: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(terms), indent=2))


class VocabWatcher:
    """Reloads vocab.json only when its mtime changes -- cheap to check on
    every request while still picking up a fresh ingest run without a
    mcp-server restart. Missing file (e.g. ingest hasn't run yet against a
    fresh volume) just means an empty vocabulary, not an error -- callers
    treat that as "typo correction has nothing to correct against"."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._mtime: float | None = None
        self._terms: set[str] = set()

    def get(self) -> set[str]:
        try:
            mtime = self._path.stat().st_mtime
        except OSError:
            return self._terms
        if mtime != self._mtime:
            self._terms = load_vocab(self._path)
            self._mtime = mtime
        return self._terms
