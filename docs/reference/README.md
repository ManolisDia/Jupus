# Jupus — Developer Reference

**This is the "how it actually works" documentation.** It describes the code as it exists on `master`, for someone who has to change it.

It is deliberately separate from the rest of `docs/`, which is a *build record* — how the project was planned and constructed, phase by phase, and why each non-obvious call was made. Those docs are still worth reading, but they are written from the perspective of someone about to build something, and several of them describe designs that were later refined or reversed. **Where this reference and a phase doc disagree, this reference wins** — it was written by reading the code, and every claim in it was checked against the code on `master`.

| You want | Read |
|---|---|
| Why the system is shaped this way | [`architecture.md`](architecture.md) |
| What happens between "caller speaks" and "row in the DB" | [`life-of-a-call.md`](life-of-a-call.md) |
| Which file owns what | [`code-map.md`](code-map.md) |
| Every field of the conversation state | [`call-state.md`](call-state.md) |
| Every node, branch and escalation trigger | [`supervisor-graph.md`](supervisor-graph.md) |
| Every tool, prompt and model choice | [`tool-catalog.md`](tool-catalog.md) |
| Tables, repositories, how to swap SQLite out | [`data-layer.md`](data-layer.md) |
| Every HTTP and WebSocket endpoint | [`api.md`](api.md) |
| Trace events, and how latency/cost are derived from them | [`tracing.md`](tracing.md) |
| The eval pipeline and the human-in-the-loop | [`eval.md`](eval.md) |
| How the tests are organised and how to add one | [`testing.md`](testing.md) |
| The caller page and the admin pages | [`frontend.md`](frontend.md) |
| "How do I add/change X?" | [`recipes.md`](recipes.md) |
| Running it, configuring it, deploying it, fixing it | [`operations.md`](operations.md) |

---

## Reading paths

**New to the project (about 45 minutes).** Read the repo [`README.md`](../../README.md) for what it does and why, then [`architecture.md`](architecture.md) for the shape, then [`life-of-a-call.md`](life-of-a-call.md) for the mechanics. That is enough to orient. Come back for [`call-state.md`](call-state.md) and [`supervisor-graph.md`](supervisor-graph.md) the first time you touch conversation logic — they are the two densest parts of the system and the two most likely to bite you.

**About to change conversation behaviour.** [`supervisor-graph.md`](supervisor-graph.md) + [`call-state.md`](call-state.md) + [`tool-catalog.md`](tool-catalog.md), then the "Change a prompt" and "Add a graph node" recipes. Anything that changes what the agent *says* should be validated through the eval loop in [`eval.md`](eval.md), not by vibes.

**About to change storage.** [`data-layer.md`](data-layer.md) alone is sufficient; the repository boundary is real and nothing outside it needs to know.

**Debugging a live call.** [`operations.md`](operations.md)'s troubleshooting table first, then [`tracing.md`](tracing.md) to read the trace, then grep [`docs/fixes/`](../fixes/INDEX.md) and [`docs/known-issues/`](../known-issues/INDEX.md) for the symptom — roughly twenty non-trivial bugs are already written up there, several of them live-only failures no unit test would have caught.

---

## What exists right now

Verified against `master` at the time of writing.

- **Fourteen phases are merged.** The four required user stories (route, capture, book, escalate) plus stretches: hosted deployment, latency instrumentation, a concurrency proof, latency reduction, and the LiveKit transport migration.
- **406 tests pass** (`backend/tests` 351, `eval/tests` 51, `backend/supervisor/knowledge/tests` 4), with no live API calls. Note that the pre-commit hook runs only the first two directories; the knowledge tests are not gated.
- **Phase 10 (telephony) was fully specified but never built.** `docs/phases/phase-10-telephony.md` is a design, not a description. There is no SIP, no Twilio, no PSTN — browser microphone only.
- **Phase 15 (polish/submission) has a doc but no branch.**

### Work in flight

There is an **unmerged branch `worktree-escalation-handoff-db`** (checked out at `.claude/worktrees/escalation-handoff-db/`) carrying an escalation-handoff feature: an `EscalationRepository`, an `escalations` table, an admin handoff-queue page, and an additive schema bootstrap. None of that is on `master`, and this reference does not document it. If you find yourself reading about a "handoff queue", that is where it lives. Escalations on `master` produce a markdown file in [`docs/handoffs/`](../handoffs/) and nothing else.

---

## Conventions used in these docs

- Paths are written as `backend/supervisor/graph.py`, relative to the repo root.
- "Node" always means a LangGraph node in `backend/supervisor/graph.py`. "Stage" always means the value of `CallState["stage"]`. They are related but not one-to-one — see [`supervisor-graph.md`](supervisor-graph.md).
- "The supervisor" means the Claude-backed LangGraph state machine. "Realtime" means the OpenAI Realtime model that does the actual speaking and listening. They are different systems with different jobs, and confusing them will make nothing else make sense.
