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
