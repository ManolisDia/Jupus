# Jupus

**An inbound voice AI receptionist for a law firm** — built as a Voice AI Engineer take-home. Callers talk to it like a person: it routes them across practice areas, captures their details even over a noisy line, answers a legal question with a real statute citation, books a consultation slot, and knows when to hand off to a human instead of guessing.

It runs entirely on your laptop. No Docker, no telephony — open a browser tab, say something, watch it work.

---

## What it actually does

You open the client page and start talking:

1. **It greets you and listens.** Speech, turn-taking, and voice all come from OpenAI's Realtime API, carried over WebRTC by LiveKit — the conversation feels live, not scripted.
2. **It figures out why you're calling.** Employment, tenancy, or immigration — classified from what you say, not a phone-tree menu.
3. **It gets your details, carefully.** Name, email, phone. If the audio was noisy or the model wasn't confident, it confirms back with you rather than silently guessing wrong. Email and phone are *always* read back, regardless of confidence.
4. **It answers your actual question.** It asks what happened, searches a small per-area statute corpus, and cites a real provision — grounded against a closed candidate set so it cannot invent one — with a "general information, not legal advice" disclaimer.
5. **It books you a real slot.** Checked and reserved against a local calendar. If your first choice is taken it offers up to three alternatives and handles the back-and-forth.
6. **It knows its limits.** Multi-area legal issues, callers who explicitly ask for a human, or repeated capture failures trigger a clean escalation with a written handoff note — instead of the bot pretending it can help.

Every one of those decisions — routing, confidence thresholds, escalation triggers, which slot to offer — is **deterministic code reasoning over LLM output**, never the LLM freelancing. That distinction is the core architectural bet of this project.

---

## Under the hood

```
Caller's browser ──WebRTC──▶ LiveKit room ──▶ Jupus agent (in-process with the backend)
                                                    │
                                                    ▼
                              OpenAI Realtime ──speech + turn-taking + voice──▶ caller hears a reply
                                                    │
                          the ONLY tool Realtime is ever given: ask_supervisor(reason, utterance)
                                                    ▼
                                        LangGraph supervisor (Claude)
                                                    │
       ┌──────────────┬──────────────┬──────────────┼──────────────┬──────────────┐
       ▼              ▼              ▼              ▼              ▼              ▼
   greeting       routing      capture_fast    research      booking       escalation
                (classify)   /capture_confirm  (cite law)  (check/book)  (handoff note)
                                                    │
                                                    ▼
                                  SQLite — calendar, call log, decision trace
```

Two vendors, deliberately. **OpenAI Realtime** handles everything about *sounding* like a conversation. A **Claude-powered LangGraph state machine** handles everything about *deciding what to do*. Realtime never makes a business decision on its own — it defers to the supervisor through a single dispatch call, every time.

### The hard rules

That split is enforced by nine architecture rules, not convention. The important ones:

- **Realtime sees exactly one tool, ever.** All business logic sits behind `ask_supervisor`. No widening the toolset "just for simple things."
- **Graph edges are plain `if`/`else` on call state.** A node never asks an LLM which node runs next.
- **Validators are code.** `validate_email` / `validate_phone` are regex, never an LLM call.
- **Every tool call is traced**, and every Claude call goes through one wrapper — which is what makes the observability below true by construction rather than by discipline.
- **No SQL outside the repository layer.** Swapping SQLite for Postgres means writing one set of classes and touching nothing else.

Two of these are enforced by pre-commit hooks; the rest by an independent agent review before any phase merges. Full rationale for every non-obvious call is in [`docs/DECISIONS.md`](docs/DECISIONS.md).

---

## Making the conversation feel fast

A voice agent lives or dies on silence. Four mechanisms attack it, and they attack *different* things — this is where most of the engineering went.

### 1. The dispatcher never blocks the caller

Supervisor calls are fire-and-forget background tasks. You can keep talking while one is in flight; the audio stream is never held up waiting for Claude.

### 2. Hide the wait behind a real question (Phases 7 & 8)

The best filler is a question you actually needed to ask.

- **Optimistic capture** — the fast sequencer asks for the *next* field immediately with zero Claude calls, while the previous field's real extraction and validation run in the background. Corrections drain in a batched confirm phase at the end. Field capture stops costing one round trip per field.
- **Case research** — the agent asks "what happened?" and runs the BM25 statute search *during* your answer.

### 3. Make the round trip itself shorter (Phase 13)

Measured, not guessed: prompt caching, merging two sequential Claude calls into one, per-tool model choice (two tools moved to Haiku), and root-causing a retry-driven latency tail — an unbounded output field was overflowing `max_tokens` mid-generation, forcing a silent retry. Net **~12.7%** off the supervisor round trip, validated by replaying the canonical scenarios before and after.

### 4. Cover what's left with a filler (Phase 14)

Three turns have no natural next question — you've just answered, and the reply *is* what you're waiting for. Those get a short pre-rendered line ("Okay, one sec.") in the same voice, scheduled on a continuous-idle dwell so a *fast* turn is never narrated and the filler can never talk over you. If the supervisor is still working four seconds later, a second line follows, so a long wait is re-acknowledged rather than promised once and abandoned.

Measured live, from real playout:

| turns | n | round trip | time to first audio |
|---|---:|---:|---:|
| with a filler | 6 | 2484ms | **422ms** |
| without one | 12 | 766ms | **1796ms** (p95: 6342ms) |

The round-trip column is Phase 13's and **did not move** — Phase 14 changed perceived wait, not actual latency, and doesn't claim otherwise.

### Interruptions

Turn-taking is deliberately **not** owned by this project — OpenAI's `semantic_vad` decides when you've finished speaking using the model's own judgement rather than a silence timer, so "umm…" mid-sentence doesn't cut you off. Barge-in is on: talk over the agent and it stops.

Talking over a *filler* is the interesting case, and it has one deterministic policy. A backchannel ("mhm", "okay") is dropped — otherwise every murmur would reroute the turn. Anything substantive ("actually it's Alex with an X") reaches the supervisor. That's a closed-token-set check, not an LLM call, because an LLM call would reintroduce exactly the latency the filler exists to hide.

---

## Knowing whether it actually works

Transcripts don't tell you if a voice agent is good. This is the machinery that does.

### The admin panel (`/admin`)

- **Call list** with outcome and error-class badges
- **Drill-in**: full transcript plus the complete decision trace — every tool call, retry, confidence score and stage transition, in order
- **Latency & cost**: p50/p95 across four stages (speech→dispatch, supervisor round trip, deferred wait, time-to-first-audio) plus real per-call cost across *both* vendors, taken from actual token counts rather than estimated from duration
- **Handoff queue** (`/admin/escalations.html`) — every escalated call, newest first: why they rang, whatever contact details were actually confirmed, and why the agent gave up. Written to the database at escalation time, not just to a markdown file
- **Live supervisor view** (`/admin/graph.html`) — watch a call move through the graph in real time
- **Concurrency stress test** (`/admin/stress-test.html`) — fire N concurrent calls and watch for cross-call state leakage
- **Raw table viewer** for debugging

### The eval pipeline

| Command | What it does |
|---|---|
| `python eval/run_eval.py --label <name>` | Deterministic stats + an LLM judge classifying every call against an editable error taxonomy (4 classes: repetition, surfaced failure, premature escalation, unconfirmed action) |
| `python eval/replay_scenarios.py --label <name>` | Drives the 7 canonical scenarios through the **real** unmocked pipeline |
| `python eval/compare_runs.py --baseline a --candidate b` | Diffs per-error-class rates between two labelled runs — this is how a prompt change gets validated |
| `python eval/calibrate_judge.py` | Scores the LLM judge against human annotations |
| `python eval/livekit_live_call.py --all` | Drives real LiveKit calls end to end with **synthesized caller speech** — no human at a mic |
| `python eval/filler_latency_report.py` | The perceived-vs-actual latency table above |
| `python eval/concurrency_stress_test.py` | N concurrent calls; proves no cross-call state leakage |

### The human in the loop

An LLM judge grading its own system is a closed loop. [`docs/benevolent_dictator.md`](docs/benevolent_dictator.md) defines a single designated annotator who reviews calls at `/admin/annotate`, whose labels are the strongest signal into taxonomy changes — and who is the **sole approver** of any change to the error taxonomy. Neither the judge nor its own self-critique can auto-apply one.

### Tests

**516 tests**, no live API calls, including all 7 canonical scenarios driven through the real dispatcher → graph → persistence path with Claude mocked. Every commit is gated on the 512 in `backend/tests` and `eval/tests`, plus a secrets scan and the architecture checks.

---

## Quickstart

**Requirements:** Python 3.11+, an OpenAI API key, an Anthropic API key, and a [LiveKit Cloud](https://cloud.livekit.io) project (the free "Build" tier is enough, no card needed).

```bash
# 1. install
pip install -e ".[dev]"
pre-commit install

# 2. configure
cp .env.example .env
# fill in OPENAI_API_KEY, ANTHROPIC_API_KEY,
# and LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET

# 3. seed the local calendar
python backend/db/seed_slots.py

# 4. run the backend (this also starts the LiveKit agent worker, in-process)
uvicorn backend.app:app --reload
```

Then **call it**: open [`client/index.html`](client/index.html) and talk. **Watch it**: `http://localhost:8000/admin`.

> Run only **one** backend at a time. LiveKit dispatches automatically to every registered worker, so a second one will silently take some of your calls — and because it keeps its own in-memory state, those calls vanish from the first one's admin panel. The backend warns about this at startup.

### The hosted deployment

A public deployment exists from Phase 9 — FastAPI on Railway (with a persistent volume for SQLite) and the client on Firebase Hosting at **https://jupus-5661c.web.app**, behind a shared-secret access token. Railway auto-deploys from `master`.

To bring it up to date after a change:

```bash
# backend: nothing to do beyond pushing — Railway redeploys from master.
# It also needs LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET set as
# Railway variables, or /livekit-token returns a 503 saying exactly that.

firebase deploy --only hosting        # client
```

The client works out its own backend from where it's served (`file://` or localhost → your local backend; anything else → Railway), so there's no config file to edit before deploying and change back afterwards. `client/config.js` is only needed to supply the hosted access token, or to point the local page somewhere unusual.

> **Run the local backend or the hosted one, not both.** They register LiveKit workers against the same project, and LiveKit dispatches to *any* registered worker — so with both up, calls are split between them at random, and a call handled by the "wrong" one won't appear in the other's admin panel or database. Give production its own LiveKit project if you need both at once.

### Try it without talking

```bash
python backend/db/seed_demo_calls.py     # canned demo calls
python eval/run_eval.py --label demo     # run the taxonomy judge over them
```

Then browse `/admin` — badges, transcripts and traces are all populated.

---

## Project layout

| Path | What's there |
|---|---|
| [`backend/transport/`](backend/transport/) | The LiveKit agent, the Realtime session config, the one tool schema, pre-rendered filler audio |
| [`backend/dispatcher.py`](backend/dispatcher.py) | Async supervisor dispatch, background verification/search reconciliation, disconnect cleanup |
| [`backend/supervisor/`](backend/supervisor/) | The LangGraph state machine: state, nodes/edges, tools, prompts, deterministic heuristics, tracing |
| [`backend/db/`](backend/db/) | Schema, seed scripts, and the repository layer (the only place SQL lives) |
| [`backend/app.py`](backend/app.py) | FastAPI: LiveKit room tokens, admin API, live trace stream |
| [`client/`](client/) | The caller-facing page — orb visualiser, live transcript, captured-details panel |
| [`admin/`](admin/) | Call list, trace drill-in, handoff queue, live graph view, annotation queue, stress test, DB viewer |
| [`eval/`](eval/) | Error taxonomy, LLM-judge insights agent, and the eval CLIs above |
| [`scripts/`](scripts/) | Pre-commit architecture/secrets checks, filler audio generation |
| [`docs/`](docs/) | Architecture, decisions, phase specs, scenarios, fixes log |

## Docs worth knowing about

**Working on the code?** Start at [`docs/reference/`](docs/reference/README.md) — a developer handbook describing the system as it actually is: the life of a call, every node and branch, every state field, the tool catalog, the schema, the API surface, the trace events, the eval pipeline, and a set of "how do I change X" recipes. It is written from the code, and it is the one place that supersedes anything below it if they disagree.

Everything else in `docs/` is the **build record** — how the project was planned and why each call was made:

- [`docs/architecture.md`](docs/architecture.md) — the four-layer shape and the repository pattern
- [`docs/DECISIONS.md`](docs/DECISIONS.md) — **why** every non-obvious call was made, including the ones tried and reversed
- [`docs/answers.md`](docs/answers.md) — the four written answers (latency, turn-taking, iteration/scale, telephony)
- [`docs/PLAN.md`](docs/PLAN.md) — the full build plan and phase index
- [`docs/scenarios.md`](docs/scenarios.md) — the 7 canonical scenarios used by manual QA, the mocked suite, and live runs alike
- [`docs/error_taxonomy.md`](docs/error_taxonomy.md) — the editable error-class registry the judge scores against
- [`docs/fixes/`](docs/fixes/) — a numbered log of every non-trivial bug and its root cause. Several are live-only failures no unit test would have caught.

---

## How it was built

Fourteen phases, each with a written spec and a strict Definition of Done, each on its own branch, each reviewed by an independent agent against that DoD before merging. The first eight cover the four required user stories; 9 and 11–14 are deliberate stretches — hosting, latency instrumentation, a concurrency proof, latency reduction, and the LiveKit transport.

The `docs/fixes/` log and the "reversed after live testing" entries in `DECISIONS.md` are the honest part of that record. A spoken filler, for instance, was built in Phase 2, tested live, removed as *worse* than silence — and only reintroduced in Phase 14 once the mechanism addressed the specific complaint that killed it the first time.

## Known limits

A scoped prototype, deliberately:

- **No telephony.** Browser mic only. Phase 10 was fully specified (SIP + Twilio warm transfer, see [`docs/phases/phase-10-telephony.md`](docs/phases/phase-10-telephony.md)) but not built — the design is written up in `docs/answers.md` instead.
- **A third external dependency: LiveKit**, alongside OpenAI and Anthropic. The free tier costs nothing, but it must be provisioned before a call will connect at all. Self-hosting was considered and rejected — a single-region SFU would likely make media latency *worse*, not better.
- **The agent worker runs inside the backend process**, so it can reach in-memory call state directly. That's what keeps the live admin view working, but it does mean one process is doing real-time media work alongside serving HTTP.
- **In-memory call state** — fine for one local process, not for horizontal scaling.
- **SQLite**, not a hosted database. The repository layer exists so that's a contained change.
- **The hosted deployment is single-instance**, has no autoscaling, and its access token is a casual-discovery deterrent rather than a real auth boundary. The local setup is the primary, always-works path. Only one of local/hosted can serve calls at a time unless they use separate LiveKit projects (see above).
- **No cap on call duration**, and the browser client has no automated test coverage.

Every one of these is a documented tradeoff in `docs/DECISIONS.md`, not an oversight.
