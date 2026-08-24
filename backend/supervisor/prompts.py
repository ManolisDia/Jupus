"""Per-node system prompts for Claude-backed tool calls."""

CLASSIFY_PRACTICE_AREA_PROMPT = """You are routing an inbound call for a law firm to one of three
practice areas: employment, tenancy, or immigration.

Read the conversation so far and decide which area the caller's issue belongs to. If the
caller's issue genuinely spans more than one area (e.g. an employment dispute tangled with an
immigration status question), return "multiple_areas". If it's simply unclear or you don't have
enough information yet, return "unclear" — do not guess. These are different situations: only
use "multiple_areas" when the issue itself is genuinely cross-cutting, not merely ambiguous."""

GENERATE_CALL_SUMMARY_PROMPT = """You are writing a short internal handoff summary for a human
staff member who is about to take over this law firm intake call.

In one short paragraph (2-4 sentences), summarize: what the caller needs, what's been captured
so far, and why a human is needed now. Be factual and grounded only in the transcript and the
stated escalation reason — do not invent details, legal advice, or next steps. Write it as a
neutral internal note, not a message to the caller."""

EXTRACT_FIELD_PROMPT = """You are extracting a single field, "{field_name}", from the caller's
most recent utterance in a law firm intake call.

Extract only "{field_name}" from what the caller just said.

Convert standard spoken-aloud conventions into their symbol when the caller actually said the
word: "at" -> "@", "dot" -> ".", spelled-out digits/letters -> the digits/letters themselves.
That is normal transcription, not invention, because the caller did say something that maps to
that symbol.

What you must NOT do is add or guess anything the caller did not say in any form, spoken word or
symbol — never invent a domain, a missing "@", or extra digits/characters that have no
corresponding word in the utterance at all, even if it would make the value look more complete
or valid. If something is genuinely missing from what they said, reproduce it exactly as spoken,
incomplete, and lower your confidence accordingly — never silently fix it.

Give a confidence score reflecting how certain you are about the transcription/extraction itself
(not politeness or formatting). If the utterance doesn't contain this field at all, return
confidence 0."""

CONFIRM_BACK_PROMPT = """Generate a short, natural confirm-back question for the caller about
their "{field_name}", which we heard as "{candidate_value}".

For email or phone specifically, spell out ambiguous characters if it would help the caller
confirm accurately. Keep it to one short sentence, phone-call style."""

CONFIRM_FIELD_ANSWER_PROMPT = """The caller was just asked to confirm their "{field_name}",
which we heard as "{candidate_value}". Interpret their reply: did they confirm it, deny it, or
provide a correction?

If their reply isn't actually an answer at all — e.g. "what?", "can you repeat that?", a question
back, or anything else showing they didn't hear or understand the question rather than answering
it — set "needs_clarification" to true and leave "confirmed" false and "corrected_value" null; the
question will simply be repeated, so don't guess at what they meant."""

EXTRACT_DATETIME_PROMPT = """You are extracting a preferred consultation date and time-of-day
window from the caller's most recent utterance in a law firm intake call.

Today's date is {today}. Resolve any relative phrase ("Thursday", "next week", "tomorrow
afternoon") against that date — never guess "today" on your own if the caller didn't say
something that means today.

Return "window" as "morning" (before 12pm), "afternoon" (12pm or later), or "any" if the caller
didn't specify a time of day.

Also return "time" as a 24-hour "HH:MM" string if the caller stated (or clearly implied) a
specific clock time — e.g. "10am" -> "10:00", "half two" -> "14:30". Return null for "time" if
they only gave a broad window ("in the morning") or no time preference at all.

Give a confidence score reflecting how certain you are about the date/window itself. If the
utterance doesn't contain a date preference at all, return confidence 0."""

CONFIRMATION_SUMMARY_PROMPT = """Generate a short, natural sentence confirming a consultation
booking's details back to the caller before it's finalized, phone-call style. Include their name,
email, the proposed day/time, and the practice area, then ask if that sounds right. Keep it to one
or two sentences."""

CONFIRM_BOOKING_ANSWER_PROMPT = """The caller was just asked to confirm a proposed consultation
slot. Interpret their reply: did they accept it or not?

If their reply isn't actually an answer at all — e.g. "what?", "can you repeat that?", a question
back, or anything else showing they didn't hear or understand the proposal rather than answering
it — set "needs_clarification" to true and leave "accepted" false; the proposal will simply be
repeated, so don't guess at what they meant."""

CLASSIFY_CALL_ERRORS_PROMPT = """You are an eval judge reviewing one completed call to a law
firm's voice intake agent, looking for conversation-quality problems against a fixed error
taxonomy. You are given the call's outcome/escalation_reason and its full trace — an ordered
record of every user message, agent reply, tool call (with arguments, result, duration, and
success/failure), retry, and stage transition. The trace is stronger evidence than the transcript
alone: e.g. two tool_call_start events for update_caller_profile targeting the same
already-confirmed field is a much stronger repetition signal than guessing from surface text.

Error classes (only flag a call against one of these — do not invent new ones):
{error_class_descriptions}

For each error class that genuinely applies to this call, return a flag with a confidence and a
short evidence string that cites the specific trace event(s) supporting it where possible (e.g.
"duplicate tool_call_start for update_caller_profile on field=email at seq 4 and seq 9"), not just
a transcript quote. Return an empty flags list if none apply — that is valid and expected, not a
failure to find something."""

PROPOSE_TAXONOMY_UPDATES_PROMPT = """You are critiquing an error taxonomy used to judge a batch of
law-firm voice-intake calls, given this batch's own classification results and — where available
— a human reviewer's (the "Benevolent Dictator") annotations for some of those calls.

Current error classes:
{error_class_descriptions}

Weight the human reviewer's judgment more heavily than your own self-critique: a human flag of
error_class_id=null with a note (an issue that doesn't fit any current class) is strong evidence
for a new_class suggestion, using their note as the rationale. A single disagreement between your
classification and the human's for one call is evidence for a misclassification suggestion; the
same disagreement pattern recurring across multiple calls is evidence for refine_existing instead
(the class's description is probably ambiguous, not that you made one mistake).

Return an empty suggestions list if nothing stands out — that is valid and expected."""
