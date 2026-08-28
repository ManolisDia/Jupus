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
{previous_attempt_note}
Give a confidence score reflecting how certain you are about the transcription/extraction itself
(not politeness or formatting). If the utterance doesn't contain this field at all, return
confidence 0."""


# Formatted into EXTRACT_FIELD_PROMPT / EXTRACT_AND_CONFIRM_FIELD_PROMPT's
# {previous_attempt_note} slot ONLY on a retry of a field that already failed
# once (graph.py's retry paths); the empty string otherwise, so the ordinary
# first-attempt prompt is byte-for-byte what it has always been.
#
# This narrows the "never invent" guard directly above it rather than relaxing
# it: every character still has to come from something the caller actually
# said, but "what the caller said" now spans both attempts. A caller spelling
# out an email routinely pauses mid-value, and the transport is deliberately
# forbidden from merging the resulting segments into one utterance
# (backend/transport/prompts.py rule 3a), so the halves can only ever be
# reunited here. Only used for email/phone, whose deterministic validator
# catches a bad stitch before it ever reaches the caller.
PREVIOUS_ATTEMPT_NOTE = """
The caller was already asked for this same field once and what they said could not be used.
That earlier attempt was:

  "{previous_attempt}"

They have now been asked again, and the utterance below is their new answer. Decide which of
these two cases you are in:

- The new answer is a COMPLETE value on its own, or contradicts the earlier attempt: use the new
  answer alone and ignore the earlier one entirely. This is the common case — treat it as the
  default unless the other case clearly applies.
- The new answer is only a FRAGMENT that slots together with the earlier attempt to form one
  single value (typically the earlier attempt is the start and the new answer is the rest, e.g.
  the part before the "@" first and the domain second): join them, in the order they were said,
  into that one value.

Never merge them into something neither utterance supports, never reorder or edit their contents
to force a fit, and never treat two competing complete values as fragments of each other. If you
are unsure which case applies, use the new answer alone and lower your confidence.
"""

CONFIRM_BACK_PROMPT = """Generate a short, natural confirm-back question for the caller about
their "{field_name}", which we heard as "{candidate_value}".

For email or phone specifically, spell out ambiguous characters if it would help the caller
confirm accurately. Keep it to one short sentence, phone-call style."""

# Phase 13 (latency reduction) — merges EXTRACT_FIELD_PROMPT and
# CONFIRM_BACK_PROMPT into one call: the model both extracts the field and
# drafts the confirm-back question about whatever it extracted, in a single
# response. node_capture discards confirm_back_phrasing when the extraction
# doesn't end up pending_confirm; this prompt still asks for it
# unconditionally, since which branch applies isn't known until after this
# response comes back.
EXTRACT_AND_CONFIRM_FIELD_PROMPT = """You are extracting a single field, "{field_name}", from the
caller's most recent utterance in a law firm intake call, and also drafting the short confirm-back
question that would be asked about whatever value you extract.

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
{previous_attempt_note}
Give a confidence score reflecting how certain you are about the transcription/extraction itself
(not politeness or formatting). If the utterance doesn't contain this field at all, return
confidence 0.

Separately, always also produce "confirm_back_phrasing": a short, natural confirm-back question
asking the caller to confirm the value you just extracted, even one you're not fully confident in
— the confirm-back is exactly what's meant to surface that uncertainty to the caller, so draft it
regardless of your own confidence score. For email or phone specifically, spell out ambiguous
characters if it would help the caller confirm accurately. Keep it to one short sentence,
phone-call style."""

# Phase 13 (latency reduction), Decision 4 — the final paragraph was added
# after a real live call showed confirm_field_answer's output_tokens
# ballooning to 201/351/(truncated, failed)/274/178 across repeated
# attempts at confirming a garbled, repeatedly-respelled email, eventually
# exceeding call_claude_json's max_tokens=512 mid-generation and producing
# invalid (truncated) JSON — the actual root cause of this tool's
# multi-second retry tail, confirmed via trace_events, not assumed. The fix
# is constraining verbosity at the source, not raising max_tokens (which
# would let the wasteful generation succeed instead of failing, but not
# make it fast).
CONFIRM_FIELD_ANSWER_PROMPT = """The caller was just asked to confirm their "{field_name}",
which we heard as "{candidate_value}". Interpret their reply: did they confirm it, deny it, or
provide a correction?

If their reply isn't actually an answer at all — e.g. "what?", "can you repeat that?", a question
back, or anything else showing they didn't hear or understand the question rather than answering
it — set "needs_clarification" to true and leave "confirmed" false and "corrected_value" null; the
question will simply be repeated, so don't guess at what they meant.

"corrected_value" must be nothing more than the corrected value itself — never an explanation,
never your reasoning about what the caller might have meant, and never a restatement of their full
spelled-out utterance. Even if the caller spelled something out slowly or repeated themselves
several times, resolve it down to the short final value alone."""

# The bare-weekday rule below is spelled out rather than left to the model's
# judgement: it used to resolve either way depending on whether that particular
# call happened to reason about it, which made "Friday" said on a Friday a coin
# flip. Formatted with today/weekday/next_same_weekday by tools.extract_datetime.
EXTRACT_DATETIME_PROMPT = """You are extracting a preferred consultation date and time-of-day
window from the caller's most recent utterance in a law firm intake call.

Today's date is {today}. Resolve any relative phrase ("Thursday", "next week", "tomorrow
afternoon") against that date — never guess "today" on your own if the caller didn't say
something that means today.

A bare weekday name means the NEXT future occurrence of that weekday. Today is a {weekday},
so "{weekday}" on its own means {next_same_weekday}, NOT {today} — a caller who meant today
would have said "today" or "this afternoon".

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

SELECT_OFFERED_SLOT_PROMPT = """The caller was just offered {count} alternative consultation slots
and asked if any of them work. The offered slots, in order, are:

{slot_list}

Interpret their reply:
- If they picked one of the offered slots, return its zero-based position in the list above as
  "selected_index". They may refer to it by full time ("10AM works"), by ordinal ("the first one",
  "the last one"), or by a bare number or fragment that matches exactly one offered time — "ten",
  "let's go with ten", "the 9:30", "half nine". An agreeable opener like "sure", "yeah" or "okay"
  followed by any such reference is still a selection, not a clarification. Only treat a bare
  number as ambiguous if it genuinely matches more than one of the options above.
- If they asked for a DIFFERENT time that is not one of the options above — "can you do Friday at
  3pm instead?", "anything later in the day?", "what about Monday?" — set "proposed_new_time" to
  true and leave "selected_index" null. This is a counter-offer, not a failure to understand: their
  requested time will be looked up fresh, so do NOT use "needs_clarification" for it.
- If they explicitly said none of those work / declined all of them without naming an alternative,
  set "declined_all" to true and leave "selected_index" null.
- If their reply isn't actually an answer at all — e.g. "what?", "can you repeat that?", or anything
  else showing they didn't hear the offer rather than answering it — set "needs_clarification" to
  true and leave the rest null/false; the offer will simply be repeated, so don't guess.

Exactly one of these four outcomes applies. Never return an index outside the offered list above."""

GROUND_STATUTE_CITATION_PROMPT = """You are helping a law firm's voice intake agent decide whether
to cite a specific statute back to a caller who has just described their situation, from a small
fixed set of candidate statutes already selected by keyword search.

You are given the caller's own words ("caller_situation") and a list of candidates, each with an
"id", "citation", and "text" (the actual statutory text). You must either:
- pick the single candidate that genuinely, substantively applies to what the caller described,
  returning its exact "id" as "selected_id" and a short (one or two sentence) natural,
  phone-call-style "spoken_framing" that references what the caller said and explains what the
  cited provision means for their situation — grounded ONLY in that candidate's own "text", never
  adding facts, numbers, or legal claims not present in it; or
- return {"selected_id": null, "spoken_framing": null} if none of the candidates genuinely fit —
  do not force a citation onto a situation it doesn't really match, and do not select a candidate
  just because it's topically adjacent.

You must NEVER select an id that is not in the given candidate list, and you must NEVER invent or
paraphrase a citation from your own general knowledge — only ever select from what's provided."""

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
