# Jupus

**An inbound voice AI receptionist for a law firm** — built as a Voice AI Engineer take-home. Callers talk to it like a person: it routes them across practice areas, captures their details even over a noisy line, books a real consultation slot, and knows when to hand off to a human instead of guessing.

It runs entirely on your laptop. No Docker, no telephony, no hosting — open a browser tab, say something, watch it work.

---

## What it actually does

Say you're a caller. You open the client page and start talking:

1. **It greets you and listens.** Speech-to-text, turn-taking, and voice all come from OpenAI's Realtime API — the conversation feels live, not scripted.
2. **It figures out why you're calling.** Employment, tenancy, or immigration — classified from what you say, not a phone-tree menu.
3. **It gets your details, carefully.** Name, email, phone, preferred time — extracted from natural speech. If the audio was noisy or the model isn't confident, it confirms back with you instead of silently guessing wrong.
4. **It books you a real slot.** Checked and reserved against a local calendar. If your first choice is taken, it offers alternatives and handles the back-and-forth.
5. **It knows its limits.** Multi-area legal issues, callers who explicitly ask for a human, or repeated capture failures all trigger a clean escalation with a written handoff note — instead of the bot pretending it can help.

Every one of those decisions — routing, extraction thresholds, escalation triggers — is deterministic code reasoning over LLM output, not the LLM freelancing. That distinction is the core architectural bet of this project (see [Architecture](#architecture) below).

## Under the hood

```
Caller's browser
   │  WebRTC (mic audio) via LiveKit
   ▼
LiveKit room  ──▶  Jupus agent (in-process with the backend)
   │
   ▼
OpenAI Realtime API  ──speech + turn-taking + voice──▶  Caller hears a reply
   │
   │  the ONLY tool Realtime is ever given: ask_supervisor(reason, utterance)
   ▼
backend/transport/livekit_agent.py  ──async dispatch──▶  LangGraph supervisor
                                                          │
                                          ┌───────────────┼────────────────┐
                                          ▼               ▼                ▼
                                      routing          capture          booking
                                   (classify area)  (extract + confirm)  (check/book slot)
                                          │               │                │
                                          └───────► escalation ◄───────────┘
                                                  (writes a handoff note)
                                                          │
                                                          ▼
                                              SQLite (calendar + call log + trace)
```

The voice layer and the reasoning layer are deliberately split across two vendors and two processes: **OpenAI Realtime** handles everything about *sounding* like a conversation, and a **Claude-powered LangGraph state machine** handles everything about *deciding what to do*. Realtime never makes a business decision on its own — it always defers to the supervisor through a single dispatch call.

That split is enforced by nine hard architecture rules — no widening Realtime's toolset, no LLM picking the next graph node, no raw SQL outside the repository layer, every tool call traced, every upstream API failure caught and degraded gracefully. The full rationale for each is in [`docs/DECISIONS.md`](docs/DECISIONS.md); the rules themselves are enforced by pre-commit hooks, not just convention.

## Watching it think: the admin panel

Every call — its transcript, its full decision trace (every tool call, retry, and stage transition, in order), and an LLM-judge classification of what went wrong if anything did — is logged and viewable at `/admin`. There's also a lightweight human-annotation flow (`/admin/annotate`) so a designated reviewer can grade calls against an editable error taxonomy, and an eval pipeline that replays six canonical scenarios through the real pipeline to catch regressions after a prompt change.

This isn't a bolt-on dashboard — it's the mechanism the project uses to answer "is this actually working," beyond eyeballing a transcript.

---

## Quickstart

**Requirements:** Python 3.11+, an OpenAI API key (Realtime), an Anthropic API key (supervisor).

```bash
# 1. install
pip install -e ".[dev]"
pre-commit install

# 2. configure
cp .env.example .env
# fill in OPENAI_API_KEY and ANTHROPIC_API_KEY

# 3. seed the local calendar
python backend/db/seed_slots.py

# 4. run the backend
uvicorn backend.app:app --reload
```

Then:
- **Call it:** open [`client/index.html`](client/index.html) in a browser and talk.
- **Watch it:** open `http://localhost:8000/admin`.

## Try it live

A real, publicly reachable deployment exists so you don't have to clone anything for a first
look: **https://jupus-5661c.web.app** (client, on Firebase Hosting) talking to a FastAPI backend
on Railway. It's gated behind an access token — ask the project owner for it, then open the URL
and use the page normally.

This is additive, not a replacement: the local setup above stays the primary, always-works path,
and is what a full evaluation should still use. The hosted deployment is single-instance, has no
autoscaling, and its access token is a casual-discovery deterrent (see
[`docs/DECISIONS.md`](docs/DECISIONS.md)'s Phase 9 entries), not a production auth boundary.

## Try it without talking

Want to see the eval tooling and admin panel without a live mic session?

```bash
python backend/db/seed_demo_calls.py         # seeds canned demo calls
python eval/run_eval.py --label demo         # runs the error-taxonomy judge over them
```

Then browse `http://localhost:8000/admin` — badges, transcripts, and traces are all populated.

## Testing

```bash
pytest backend/tests                          # unit + the 6-scenario regression suite, no live API calls

python eval/replay_scenarios.py --label baseline        # drive the 6 scenarios through the REAL pipeline
python eval/compare_runs.py --baseline a --candidate b  # diff error rates between two labeled eval runs
python eval/calibrate_judge.py                           # LLM judge vs. human annotations
```

---

## Project layout

| Path | What's there |
|---|---|
| [`backend/app.py`](backend/app.py) | FastAPI: session tokens, the WebSocket tool-call bridge, admin API |
| [`backend/dispatcher.py`](backend/dispatcher.py) | Async supervisor dispatch — never blocks the caller's audio stream |
| [`backend/supervisor/`](backend/supervisor/) | The LangGraph state machine: state, nodes/edges, tools, prompts |
| [`backend/db/`](backend/db/) | Schema, seed scripts, and the repository layer (the only place SQL lives) |
| [`client/`](client/) | The caller-facing browser page |
| [`admin/`](admin/) | Calls list, transcript/trace drill-in, eval summary, annotation queue |
| [`eval/`](eval/) | The error taxonomy, the LLM-judge insights agent, and the eval CLIs |
| [`docs/`](docs/) | Architecture, decisions, phase specs, scenario definitions, fixes log |

## Docs worth knowing about

- [`docs/architecture.md`](docs/architecture.md) — the four-layer shape and repository pattern
- [`docs/DECISIONS.md`](docs/DECISIONS.md) — why the non-obvious calls were made
- [`docs/PLAN.md`](docs/PLAN.md) — the full build plan and phase index
- [`docs/scenarios.md`](docs/scenarios.md) — the 6 canonical conversation scenarios used everywhere from manual QA to regression tests
- [`docs/error_taxonomy.md`](docs/error_taxonomy.md) — the editable error-class registry the eval judge scores against
- [`docs/benevolent_dictator.md`](docs/benevolent_dictator.md) — the human-annotation and taxonomy-approval process

## Known limits

This is a scoped take-home prototype, deliberately: no telephony (browser mic only), no session/call duration cap, in-memory call state (fine for a single local process, not for horizontal scaling), SQLite rather than a hosted database. Every one of these is an explicit, documented tradeoff — see `docs/DECISIONS.md` — not an oversight.

**A third external dependency: LiveKit.** Since Phase 14 the call's transport is LiveKit Agents (hosting the same OpenAI Realtime model through LiveKit's plugin), which means the project now depends on a LiveKit Cloud account alongside OpenAI and Anthropic. The free "Build" tier is enough to run everything here and needs no card, but this is still a third external service that has to be provisioned and configured (`LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`) before a call will connect at all. Self-hosting the open-source LiveKit server was considered and rejected — see `docs/DECISIONS.md` for why it would likely make media latency *worse*, not better.

Two operational sharp edges worth knowing, both of which fail silently rather than loudly:

- **LiveKit dispatches agents automatically to every room in a project.** If two backends are running against the same LiveKit credentials, they will split calls between them at random, and because each holds its own in-memory call state, a call handled by the "wrong" one simply won't appear in the other's admin panel or database. The backend logs a warning about this at startup.
- **The agent worker runs inside the backend process**, not as a separate `lk agent` deployment, so that it can reach the supervisor's in-memory state directly. That is what keeps the admin panel's live view working, but it does mean the backend process is doing real-time media work alongside serving HTTP.
