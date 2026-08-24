"""Static per-practice-area statute corpora (Phase 8 — case research).

Plain JSON, loaded once per process and cached — not a repository, not
SQLite; this is reference data in the same category as
eval/error_classes.py, not something CLAUDE.md rule #9 applies to.
"""

import json
from pathlib import Path
from typing import TypedDict


class StatuteEntry(TypedDict):
    id: str
    citation: str
    jurisdiction: str
    topic_tags: list[str]
    text: str


KNOWLEDGE_DIR = Path(__file__).resolve().parent

_CACHE: dict[str, list[StatuteEntry]] = {}


def load_corpus(area: str) -> list[StatuteEntry]:
    if area not in _CACHE:
        path = KNOWLEDGE_DIR / f"{area}_statutes.json"
        with path.open(encoding="utf-8") as f:
            _CACHE[area] = json.load(f)
    return _CACHE[area]
