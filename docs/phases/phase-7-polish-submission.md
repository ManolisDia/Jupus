# Phase 7 — Polish + Submission

## Goal

Turn a working prototype into a submittable package: a README a stranger can follow with zero undocumented steps, written answers that reference real code, a full regression pass across everything built in Phases 1–6, and a recorded walkthrough. This phase has no new application code — it's verification, documentation, and (only if time allows) the Railway stretch.

## Prerequisite
Phase 6a, 6b, and 6c DoD all met.

---

## `README.md` — required sections

1. **What this is** — 2–3 sentences, law-firm inbound voice agent take-home.
2. **Architecture summary** — the thin-Realtime/supervisor split, one paragraph, link to `docs/PLAN.md` for full detail.
3. **Required keys** — `OPENAI_API_KEY` (Realtime conversation layer) and `ANTHROPIC_API_KEY` (LangGraph supervisor reasoning) — state plainly that both are required, both are paid APIs, and give a rough sense of cost per test call so a reviewer isn't surprised.
4. **Setup steps** — exact commands, in order: clone, `pip install -e ".[dev]"`, copy `.env.example` to `.env` and fill in keys, `python backend/db/seed_slots.py`, `uvicorn backend.app:app --reload`, open `client/index.html`.
5. **Running the admin panel** — `http://localhost:{port}/admin`, and how to populate it with demo data without a live call (`python backend/db/seed_demo_calls.py && python eval/run_eval.py`).
6. **Running tests** — `pytest`.
7. **Known limitations / corners cut** — pull directly from `docs/DECISIONS.md` plus the specific ones called out per-phase (single-field-per-turn capture, keyword-based explicit-request detection, latency metric not accounting for deferred-queue wait, no telephony). Be honest and specific — the brief explicitly asks for this and explicitly says not to treat STT/LLM/TTS as magic black boxes.

## `docs/answers.md` — required structure

One section per question, each referencing actual file paths and specific behavior, not abstract description:

- **Q1 (latency/pipeline)** — reference `backend/dispatcher.py`'s fire-and-forget design and the `Phase 5` async tests; reference that Realtime handles STT+dialogue+TTS in one hop and only the supervisor detour adds a second hop, deliberately kept off the hot path.
- **Q2 (turn-taking/interruptions)** — reference reliance on OpenAI Realtime's built-in VAD/truncation (`docs/DECISIONS.md`'s entry on this), the `SPEAKING` flag mechanism in `dispatcher.py`, and the `heuristics.py` explicit-request short-circuit as a form of "the caller can interrupt the whole flow, not just a sentence."
- **Q3 (iteration/scaling/health)** — reference `eval/insights_agent.py` and the `/admin` panel directly — this is the one question where you can show, not just tell: pull up the admin panel in the video and point at a flagged call.
- **Q4 (telephony/warm transfer)** — this was explicitly out of scope for the build, so this answer is design-only. Sketch: SIP trunk (e.g. Twilio/Telnyx) bridging into the same `ask_supervisor`-gated architecture, a warm transfer as a second outbound leg dialed by the backend once `escalate_to_human` fires, briefing the human via a short synthesized/text summary (reuse `generate_call_summary`) before bridging the caller in. Cover failure modes explicitly: no answer (fall back to the existing handoff-note + voicemail-style message), busy (same fallback, maybe queue/retry once), line drops mid-bridge (detect via SIP BYE/hangup event, agent re-engages the caller rather than silently dropping them — "it looks like we got disconnected from our team, let me try again" — with a cap on retry attempts before falling back to a callback promise). Note detection at the SIP/signaling layer (486 Busy, no-answer timeout, BYE) versus the application layer (your own timeout on the bridge attempt) as two distinct places errors can surface, and that both need explicit handling, not just one.

## Final regression pass

1. `pytest` (the **entire** suite, all phases) — must pass with zero failures. This is the single command that proves the whole backend still works after Phase 7's inevitable small tweaks (README-driven fixes, tightened prompts, etc.).
2. Re-run **all 6 scripted scenarios live**, fresh, in one sitting, after everything else is finalized (catches regressions introduced while polishing):
   - Info-only, no booking (routing story, declines to book)
   - Happy-path booking, full confirm-back
   - Slot-conflict booking (pre-seeded taken slot → alternative offered and accepted)
   - Low-confidence capture (mumbled email → confirm-back → corrected)
   - Model-judged escalation (multi-area issue → `out_of_scope_multi_area`)
   - Explicit-request escalation ("get me a person" → immediate handoff)
3. **Clean-checkout dry run**: in a separate temp directory, `git clone` the repo fresh, copy `.env.example` → `.env`, fill in keys, follow the README exactly as written, nothing else. If anything doesn't work or isn't documented, fix the README (or the code) before submitting — this is exactly what the evaluator will do.

## Video (Loom) checklist
- [ ] Live demo: at least 2 of the 6 scenarios run for real on camera, including the async "caller keeps talking mid tool-call" one from Phase 5 — this is the differentiator, don't skip it.
- [ ] Architecture walkthrough: the thin-Realtime/supervisor split, why (`docs/DECISIONS.md`), the tool catalog, the confidence-threshold capture flow.
- [ ] Admin panel walkthrough for Q3, pointing at a real flagged call.
- [ ] All 4 written questions answered on camera (or read from `docs/answers.md`), referencing the actual files shown on screen.
- [ ] Honest limitations section — say what was cut and why, out loud, not just in the README.

## Optional stretch — Railway hosting
**Only attempt this if every item above is already done and there's genuinely time left.** Per `docs/DECISIONS.md`: gate the deployed endpoint behind a shared-secret token (not committed to the repo), set a hard OpenAI spend cap/alert on the account first, and keep the local path as the primary, always-working submission — the hosted version is a convenience add-on mentioned in the README, never a dependency.

## Optional stretch — Live "supervisor mind" visualization

**Goal**: a real-time visual of a call moving through the LangGraph nodes, with the supervisor's actual reasoning/tool calls surfaced on screen as they happen — for use in the video's architecture-walkthrough beat. Of everything in this doc, this is the one most likely to genuinely stand out: most take-home submissions describe their graph after the fact in slides or narration; almost none show it executing live.

**Data source — reuse, don't rebuild.** Every tool call already flows through `traced_call` into `trace_events` (rule #8, `docs/phases/cross-cutting.md` section 0), so this is a rendering layer on top of instrumentation that already exists, not a new logging path. Reads must go through `TraceRepository` (rule #9), same as the admin panel.

**Approach**: a dedicated read-only WS (e.g. `WS /admin/trace/{call_id}`) streams each `trace_events` row to the browser as it's written, so a panel can render nodes lighting up *during* the call rather than only in a post-hoc replay.

**Visual — make it sick, not just functional**:
- The `CallState` machine (Greeting → Routing → Capture → Booking → Escalation, per `docs/architecture.md`/`docs/diagrams.md`) rendered as an animated node graph on a dark canvas.
- The active node pulses/glows while the supervisor is working it; edges light up on the specific deterministic conditional that fired — e.g. show "confidence 0.62 < 0.75 → re-prompt" as the literal condition evaluated, not a vague "thinking" spinner. This directly demonstrates rule #2 (no LLM ever picks the next node) instead of just asserting it in the README.
- A side panel streams the raw reasoning / tool-call args + result for the current node live, monospace, like a real-time trace of the graph's internal state — distinct from the caller-facing transcript the admin panel already shows.
- Think "live brain scan of the agent," not a static architecture diagram with an arrow that moves.

**Placement**: a separate spectator page (e.g. `admin/graph.html` or a new admin tab), never folded into the caller-facing `client/index.html` — it's read-only, driven off the trace stream, and must have zero ability to affect the live call or add latency/risk to the hot path.

**Scope guard**: attempt only after every required Phase 7 DoD item is met, and after the Railway stretch if that's also being attempted. If time runs out, cut this before cutting anything in the required DoD — it's garnish, the working prototype is the meal.

## Optional stretch — Caller-facing visual polish

**Goal**: `client/index.html` is spec'd in Phase 1 as deliberately minimal (Start/End buttons, a status line, an empty transcript div) — appropriate for building the pipeline, but it's also the screen the evaluator watches live for the longest single stretch of the video. This stretch gives it a real visual treatment without touching the call logic underneath.

**Scope — purely presentational, zero new call-path risk.** No changes to `client/app.js`'s WebRTC/data-channel/`/bridge` logic; this only changes what's rendered from state that already exists.

- **Reactive voice visualizer**: a Web Audio API `AnalyserNode` tapped off the local mic stream and the remote (agent) audio track, driving a live amplitude/waveform animation — canvas or CSS, no new dependency. Distinct visual state per speaker (caller talking vs. agent talking vs. silence) makes turn-taking visible, not just audible.
- **Animated status states**: idle / connecting / connected / listening / speaking as distinct, smoothly-transitioning visual states rather than a plain text line.
- **Live transcript with confidence highlighting**: as fields are captured (name, email, phone, matter type), render them with a visual treatment tied to `*_confidence` — e.g. a field that triggered the re-prompt/confirm path in user story 3 is visibly flagged, so the low-confidence path (the thing the brief specifically asks to see) is legible on screen the instant it happens, not just narrated afterward.
- **Single dark, centered call-screen layout** — consistent visual language with the graph-viz panel above, so the two feel like one product rather than a prototype bolted onto a debug tool.

**Placement**: this is `client/index.html` + its CSS/JS only — no backend changes, no new endpoints.

**Scope guard**: same as the graph-viz stretch above — attempt only once the required Phase 7 DoD is met, and cut first if time runs short.

## Optional stretch — Admin panel UI/UX polish

**Goal**: `admin/index.html` is currently a plain, functional dashboard — dense HTML tables, minimal inline CSS, no visual hierarchy beyond a few pastel status badges (see current state: flat `#f6f7f9`/white panels, a bare `<table>` for the calls list, unstyled buttons in the taxonomy-suggestions row). It works, but it's also the surface the video spends real time on for Q3 (pointing at a flagged call) and the taxonomy/annotation walkthrough, so it's worth a real visual pass, not just the functional layer it has now.

**Scope — presentational and structural, but this one *can* touch layout/interaction, not just paint.** Unlike the caller-facing client stretch below (zero call-path risk because it's live-call-adjacent), the admin panel has no live-call risk at all — it's a read-only reporting surface hitting `/api/*` GET routes — so there's more room here to also improve information architecture, not just restyle what exists:

- **Visual system**: a real dark or light design language (pick one, consistent with whatever direction the graph-viz/client stretches go for a unified product feel) — proper spacing/typography scale, not system-default table styling.
- **Calls list**: better scannability than a dense table — status badges are already there conceptually (booked/escalated/info_only/abandoned), give them a stronger visual treatment; consider filtering/sorting (by outcome, by error class, by reviewed/unreviewed) since this list will only grow.
- **Trace viewer**: currently a flat monospace event log (`.trace-event`) — this is the "show me exactly what happened" view for the video, worth a real timeline treatment (indent by node, visually distinguish `tool_call_start`/`tool_call_end` pairs, highlight `reply_deferred`/`reply_delivered`/`wait_ms` events since those are the async-story payoff).
- **Taxonomy-suggestions panel + annotation queue** (`admin/annotate.html`/`annotate.js`): currently bare rows with inline buttons — give approve/reject clearer visual weight (this is a human-in-the-loop decision, it should look like one), and the annotation form itself (class checkboxes, gold flag, notes) more clearly a distinct "you are reviewing" mode.
- **Empty/loading states**: currently a single generic `.empty-state` div — differentiate "nothing selected yet" from "no calls at all" from "loading."

**Constraint**: no changes to what data is fetched or how (`admin/app.js`'s `fetch(...)` calls against `/api/*` stay as-is) — this is HTML/CSS/JS rendering polish over the existing API surface, not new backend routes or data.

**Placement**: `admin/index.html`, `admin/app.js`, `admin/annotate.html`, `admin/annotate.js` — no backend changes.

**Scope guard**: same as the other optional stretches — attempt only once the required Phase 7 DoD is met, and cut first if time runs short (alongside the caller-facing client polish below — treat the two as a pair if only one gets done, since a polished client next to a plain admin panel, or vice versa, undercuts the "one connected product" impression more than either being merely functional would).

## Optional stretch — Real statute citation (tenancy)

**Goal**: give the tenancy path a small, real, curated knowledge base of actual eviction-law text, so when a caller describes their situation the agent can retrieve and cite the specific provision that applies — a concrete, checkable answer instead of a generic "that sounds like it could be tenancy law." For a test project only, not for real callers to rely on; always spoken with a "general information, not legal advice" disclaimer, and scoped to one practice area rather than all three.

**Corpus**: 10–20 real, verbatim statute/regulation excerpts on eviction (e.g. required notice periods, valid grounds for eviction, tenant response rights) for a single, clearly-stated jurisdiction. Stored as plain data — `backend/supervisor/knowledge/tenancy_statutes.json` (or similar), each entry `{id, citation, jurisdiction, text}` — sourced and reviewed by hand, not scraped or generated at runtime.

**Retrieval — local and free, no new paid dependency.** Given the corpus is only 10–20 short entries, keyword/BM25 match over the entries is enough; a local sentence-transformer embedding + cosine search is a fine upgrade if keyword match proves too brittle, but skip a vector DB — total overkill at this corpus size.

**Wiring — fits the existing tool-scoping rule, doesn't widen it.** A new tool, e.g. `cite_law_provision(query: str) -> {citation, text} | None`, added to `tools.py`, bound only to the tenancy node's tool subset (rule #5) — the employment/immigration nodes never see it, and Realtime still only ever sees `ask_supervisor` (rule #1). Deterministic retrieval, not an LLM call, so it goes through `traced_call` directly rather than `call_claude_tool` (rule #8) — no reason to route a keyword/embedding lookup through Claude.

**Disclaimer is not optional.** Whenever this tool fires, the node's reply must include a spoken "this is general information, not legal advice — worth confirming with the attorney" line — bake this into the tenancy node's prompt template, not left to the model's discretion.

**Jurisdiction caveat**: pick one real jurisdiction, state it explicitly in the corpus and in `docs/DECISIONS.md`, and don't imply the agent covers anywhere else.

## Optional stretch — Follow-up call

**Goal**: a second, distinct call flow that runs *after* the caller has already had an offline meeting with a lawyer. The lawyer logs structured notes/action items in the admin panel; the follow-up call is the agent walking the caller back through exactly those items by voice — confirming what was discussed, and driving whatever concrete next step the lawyer specified. This is the one stretch that touches the story end-to-end (booking → real meeting → structured follow-through), so it's high-value if time allows, but it's also the most build-heavy of the stretches — treat it as last in line after the others above.

**Hard constraint — confirms fixed facts, never improvises them.** The agent must only ever speak/act on structured fields the lawyer explicitly entered in the admin panel (next steps, documents needed, a deadline date, a reschedule-needed flag) — it must never free-associate about "what the lawyer probably meant" or answer a substantive legal question that isn't already captured in those fields. Anything outside the logged fields is out of scope by design and routes straight to escalation, same as any other out-of-scope request. This keeps the feature consistent with the rest of the architecture doctrine (deterministic gating on structured state, not open-ended LLM judgment) rather than opening a second, less-guarded surface for the agent to "just talk."

**What the lawyer logs (new admin panel input, per call)** — a small structured form, not free text the agent has to interpret:
- `summary_note` (free text, spoken back verbatim as "here's what we discussed," not reasoned over)
- `next_steps: list[{type, detail}]` where `type` is a fixed enum: `confirm_appointment`, `send_documents`, `request_information`, `reschedule_needed`, `deadline_reminder` (date + description)
- each `next_step` has a `resolved: bool` the follow-up call flips to `true` once handled, so a second follow-up call (or an admin panel view) can show what's outstanding

**Other things a follow-up call could reasonably drive**, beyond what's already listed above — worth picking 2–3 to actually build rather than all of them:
- **Fee/retainer confirmation** — reading back key terms of an engagement letter and confirming receipt/understanding, not negotiating terms.
- **Rescheduling** — reuses the existing `SlotRepository`/booking-conflict machinery from Phase 4 directly; this is the cheapest of the bunch to add given what's already built.
- **Document collection** (inbound), not just delivery — "the lawyer needs your tenancy agreement before the next meeting, want me to text/email you an upload link?" — the link-sending itself can be a stub (log the intent, don't actually build file upload).
- **Case-status read-back** for longer-running matters — literally reading `summary_note`/a status field back, no reasoning about case state.
- **A structured "do you have new questions" gate** — if the caller raises something new mid-call, that's an immediate escalation, not a chance for the agent to improvise an answer.

**Graph shape**: a second, separate `CallState`-style flow (e.g. `FollowUpCallState`) with its own small node set (`greeting_followup` → `confirm_summary` → `walk_next_steps` → `ended`/`escalation`), reusing the existing tool-scoping and tracing machinery (rules #5, #8) rather than widening the primary call graph's nodes. A follow-up call is triggered by a distinct `call_id`/`call_type="follow_up"` linked back to the original `call_id`, not a new stage bolted onto the original graph.

**Tools — new, node-scoped, deterministic where possible**: `get_followup_items(call_id) -> {summary_note, next_steps}` (repository read, no LLM), `mark_step_resolved(call_id, step_index)` (deterministic write), plus reuse of the existing booking tools for the `confirm_appointment`/`reschedule_needed` cases. Only the confirm-back conversational phrasing goes through Claude — the fetch/write of what's actually true stays in code, not the model's memory of the conversation.

**Demo value**: this is a strong second scenario for the video specifically because it shows the admin panel and the voice agent as one connected loop — lawyer enters structured notes, caller calls back, agent reads them out and drives action — rather than the admin panel being a read-only reporting surface.

## Optional stretch — Easter egg: joke confession
**Fun, not required, and deliberately not part of the evaluator-facing demo** — a personal touch for when you're showing this to friends, not something to feature in the submitted video (it's off-brief and would read as unprofessional to an evaluator; keep it un-triggered/unmentioned in the Loom recording).

**Behavior**: if the caller says something absurdly self-incriminating as an obvious joke (e.g. "I just murdered someone," "I robbed a bank"), the agent responds with a fixed line — *"Oh very funny, Manos told me you might try and play a trick on me — joke's on you, I'm calling the police"* — and the browser shows a brief flashing police-siren visual effect.

**Detection — deterministic, not LLM-based, and deliberately narrow.** Add `is_joke_confession(utterance: str) -> bool` to `backend/supervisor/heuristics.py`, alongside `is_explicit_human_request`: a short, specific phrase list ("i murdered someone," "i just killed someone," "i robbed a bank," "i committed a crime," etc.), lowercase substring match. Not an LLM classifier — this is purely for fun, and an LLM call adds latency/cost/risk of misfiring for something this frivolous. Keep the phrase list narrow and literal enough that it will never plausibly match a genuine, serious disclosure of harm — if there's any ambiguity about whether a phrase could be real, leave it out rather than risk a joke response to something that isn't a joke.

**Where it's checked**: in `dispatcher.process_supervisor_call`, checked early — same priority tier as `is_explicit_human_request` — before the utterance reaches the graph at all. On match: skip `GRAPH.invoke` entirely, return the fixed line as `pending_reply`, and separately send `{"type": "easter_egg", "effect": "police_siren"}` over `/bridge` for the client to render.

**Must not pollute the eval/taxonomy system.** This is the one thing worth being careful about: do **not** route this through the normal `escalation_reason`/`outcome` machinery — Phase 6a-6c's deterministic stats and the LLM judge assume every call is a real business interaction, and a joke case showing up as an `escalation_reason` or getting classified against the error taxonomy would be actively misleading. Give it its own distinct `calls.outcome` value (e.g. `"easter_egg"`) and explicitly exclude that value from `booking_success_rate`'s and `escalation_reason_histogram`'s denominators.

**Client**: `admin`/`client/app.js`'s `ws.onmessage` handles the new `"easter_egg"` message type — a few seconds of a flashing red/blue overlay (plain CSS, no new dependency), then clears automatically.

**Tests** (light, matching the tone of the feature): `test_is_joke_confession_matches_known_phrases`, `test_is_joke_confession_does_not_match_genuine_distress` (a couple of serious, non-joke utterances that must **not** match — this is the important one), `test_joke_confession_short_circuits_before_graph_invoke`, `test_joke_confession_outcome_excluded_from_deterministic_stats`.

---

## Definition of Done
- [ ] `pytest` (full suite) passes with zero failures.
- [ ] All 6 scenarios re-run live, fresh, and pass.
- [ ] Clean-checkout dry run succeeds following only the README.
- [ ] `docs/answers.md` complete, all 4 answers reference real files/behavior.
- [ ] README complete per the sections above.
- [ ] Video recorded and covers every item in the checklist above.
- [ ] `docs/fixes/` and `docs/known-issues/` reviewed one last time — anything still open in `known-issues/` should be mentioned as a known limitation in the README, not silently left undocumented.
