"""Small static FAQ knowledge base for deflecting caller side-questions.

Deterministic keyword matching, not a Claude call — the knowledge base is
tiny and fixed, so there's no need to spend an LLM round-trip classifying
against it (same reasoning as heuristics.py's is_explicit_human_request).
Used by graph.py to answer a genuine tangent honestly before steering the
caller back to whatever the current node actually needs from them.
"""

FAQ_ENTRIES: list[dict] = [
    {
        "keywords": ["weekend", "saturday", "sunday"],
        "answer": "We're open Monday to Friday, 9am to 5pm — closed on weekends.",
    },
    {
        "keywords": ["office", "address", "location", "where are you", "where's your"],
        "answer": "Our office is at 123 Example Street.",
    },
    {
        "keywords": ["how much", "cost", "fee", "price", "charge"],
        "answer": "Consultation fees vary by case — the lawyer you're booked with will go over pricing directly.",
    },
    {
        "keywords": ["how long", "duration", "how much time"],
        "answer": "Consultations are typically 30 minutes.",
    },
]


def match_faq(utterance: str) -> str | None:
    lowered = utterance.lower()
    for entry in FAQ_ENTRIES:
        if any(keyword in lowered for keyword in entry["keywords"]):
            return entry["answer"]
    return None
