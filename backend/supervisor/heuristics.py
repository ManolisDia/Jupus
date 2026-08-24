"""Deterministic (non-Claude) heuristics used by the dispatcher."""

EXPLICIT_REQUEST_PHRASES = [
    "speak to a person", "talk to a human", "real person",
    "representative", "talk to someone", "human agent",
    "speak with someone", "get me a person", "transfer me",
    "speak to someone else", "human being",
]


def is_explicit_human_request(utterance: str) -> bool:
    lowered = utterance.lower()
    return any(phrase in lowered for phrase in EXPLICIT_REQUEST_PHRASES)
