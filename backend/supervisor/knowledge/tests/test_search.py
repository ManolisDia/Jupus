"""Phase 8 (case research) — direct unit coverage for the BM25 retrieval
unit itself, independent of the graph/dispatcher wiring around it. See
docs/phases/phase-8-legal-research.md."""

from backend.supervisor import tools
from backend.supervisor.knowledge.corpus import load_corpus
from backend.supervisor.knowledge.search import bm25_search


def test_bm25_search_ranks_relevant_entry_top():
    corpus = load_corpus("tenancy")
    results = bm25_search(
        "My landlord is trying to evict me tomorrow without giving me any notice.", corpus, top_k=3
    )
    assert results
    top_ids = {r["id"] for r in results}
    # Either the notice-to-quit or the section 21 no-fault notice entry is a
    # legitimate top match for this utterance — the closed-set grounding
    # call (tools.ground_statute_citation) is what picks the best-fitting
    # one of these, not BM25 alone.
    assert top_ids & {"tenancy-poe1977-s5", "tenancy-ha1988-s21"}
    assert results[0]["score"] >= tools.BM25_RELEVANCE_FLOOR


def test_bm25_search_low_score_for_irrelevant_query():
    corpus = load_corpus("tenancy")
    results = bm25_search("do you offer payment plans for the consultation fee", corpus, top_k=3)
    assert not results or results[0]["score"] < tools.BM25_RELEVANCE_FLOOR


def test_bm25_search_empty_corpus_or_query_returns_empty():
    corpus = load_corpus("employment")
    assert bm25_search("", corpus, top_k=3) == []
    assert bm25_search("something", [], top_k=3) == []


def test_load_corpus_entries_have_unique_ids_and_required_fields():
    for area in ("employment", "tenancy", "immigration"):
        corpus = load_corpus(area)
        assert 1 <= len(corpus)
        ids = [entry["id"] for entry in corpus]
        assert len(ids) == len(set(ids))
        for entry in corpus:
            assert entry["citation"]
            assert entry["text"]
            assert entry["jurisdiction"]
            assert entry["topic_tags"]
