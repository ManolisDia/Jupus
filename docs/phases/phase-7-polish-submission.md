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

---

## Definition of Done
- [ ] `pytest` (full suite) passes with zero failures.
- [ ] All 6 scenarios re-run live, fresh, and pass.
- [ ] Clean-checkout dry run succeeds following only the README.
- [ ] `docs/answers.md` complete, all 4 answers reference real files/behavior.
- [ ] README complete per the sections above.
- [ ] Video recorded and covers every item in the checklist above.
- [ ] `docs/fixes/` and `docs/known-issues/` reviewed one last time — anything still open in `known-issues/` should be mentioned as a known limitation in the README, not silently left undocumented.
