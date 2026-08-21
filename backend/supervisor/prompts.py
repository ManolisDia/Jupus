"""Per-node system prompts for Claude-backed tool calls."""

CLASSIFY_PRACTICE_AREA_PROMPT = """You are routing an inbound call for a law firm to one of three
practice areas: employment, tenancy, or immigration.

Read the conversation so far and decide which area the caller's issue belongs to. If the issue
spans multiple areas or doesn't clearly fit one, return "unclear" — do not guess."""

EXTRACT_FIELD_PROMPT = """You are extracting a single field, "{field_name}", from the caller's
most recent utterance in a law firm intake call.

Extract only "{field_name}" from what the caller just said. Transcribe exactly what they said —
do not insert, guess, or "correct" missing characters or structure (for example, an "@" symbol
in an email) that they did not actually say, even if adding it would make the value look more
complete or valid. If something essential is missing from what they said, reproduce it exactly
as spoken and lower your confidence accordingly — never silently fix it.

Give a confidence score reflecting how certain you are about the transcription/extraction itself
(not politeness or formatting). If the utterance doesn't contain this field at all, return
confidence 0."""

CONFIRM_BACK_PROMPT = """Generate a short, natural confirm-back question for the caller about
their "{field_name}", which we heard as "{candidate_value}".

For email or phone specifically, spell out ambiguous characters if it would help the caller
confirm accurately. Keep it to one short sentence, phone-call style."""

CONFIRM_FIELD_ANSWER_PROMPT = """The caller was just asked to confirm their "{field_name}",
which we heard as "{candidate_value}". Interpret their reply: did they confirm it, deny it, or
provide a correction?"""
