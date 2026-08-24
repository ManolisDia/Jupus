"""Plain-Python BM25 keyword search over a practice area's statute corpus
(Phase 8 — case research). Deliberately not embeddings/a vector DB — see
Decision 1/7 in docs/phases/phase-8-legal-research.md: at 8-12 entries per
area this is the right-sized retrieval mechanism, a "smart" rank over the
corpus rather than a linear scan, without a new paid or heavy dependency.
An embedding-based upgrade is a flagged future consideration, not built now.
"""

import math
import re
from collections import Counter

from backend.supervisor.knowledge.corpus import StatuteEntry

BM25_K1 = 1.5
BM25_B = 0.75

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# A short, high-frequency stopword list. Without this, common function
# words (e.g. "to", "is", "my") occasionally overlap enough between an
# unrelated query and some entry's phrasing to push its BM25 score above
# BM25_RELEVANCE_FLOOR purely by chance — confirmed while calibrating the
# floor against deliberately off-topic queries during Phase 8. Small and
# deliberately conservative: only removes words with no discriminative
# value for this corpus, not a general-purpose NLP stopword list.
_STOPWORDS = frozenset(
    "a an the is it to and of in on for that this i you do does did what "
    "when where who which how my your me am are was were be been not no "
    "so if but or at as with from by just really get got going have has "
    "had will would can could should about there here".split()
)


def _tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOPWORDS]


def _entry_text(entry: StatuteEntry) -> str:
    return entry["text"] + " " + " ".join(entry["topic_tags"])


def bm25_search(query: str, corpus: list[StatuteEntry], top_k: int = 3) -> list[dict]:
    """Ranks `corpus` against `query` via Okapi BM25, returns the top_k
    entries each merged with a "score" key, highest first. An empty
    corpus, an empty/unrecognizable query, or a corpus none of whose
    entries share any vocabulary with the query all legitimately produce
    an empty or all-zero-score result — most utterances during the
    research stage won't be about any statute at all, and that's expected,
    not an error.
    """
    query_tokens = _tokenize(query)
    if not query_tokens or not corpus:
        return []

    doc_tokens = [_tokenize(_entry_text(entry)) for entry in corpus]
    doc_lens = [len(tokens) for tokens in doc_tokens]
    avg_len = sum(doc_lens) / len(doc_lens)

    doc_freq: Counter = Counter()
    for tokens in doc_tokens:
        doc_freq.update(set(tokens))

    n_docs = len(corpus)
    scored = []
    for entry, tokens, doc_len in zip(corpus, doc_tokens, doc_lens):
        term_freq = Counter(tokens)
        score = 0.0
        for term in set(query_tokens):
            if term not in term_freq:
                continue
            df = doc_freq.get(term, 0)
            idf = math.log((n_docs - df + 0.5) / (df + 0.5) + 1)
            tf = term_freq[term]
            denom = tf + BM25_K1 * (1 - BM25_B + BM25_B * doc_len / avg_len)
            score += idf * (tf * (BM25_K1 + 1)) / denom
        scored.append({**entry, "score": score})

    scored.sort(key=lambda e: e["score"], reverse=True)
    return scored[:top_k]
