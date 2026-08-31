# Jupus

An inbound voice agent for a law firm, built as a take-home. You call it in a browser and talk. It works out what area of law you need, takes your details, answers a legal question with a real statute citation, books you a consultation, and hands you to a human when it shouldn't be guessing.

It runs on your laptop. No Docker, no phone line.

## Running it

You need Python 3.11+, an OpenAI API key, an Anthropic API key, and a [LiveKit Cloud](https://cloud.livekit.io) project. The LiveKit free tier is enough and doesn't need a card. The two API keys are paid; a full call costs about $0.50, almost all of it OpenAI Realtime.

```bash
pip install -e ".[dev]"

cp .env.example .env
# fill in OPENAI_API_KEY, ANTHROPIC_API_KEY,
# and LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET

python backend/db/seed_slots.py
uvicorn backend.app:app --reload
```

Then open `client/index.html` and talk. Watch what it's doing at `http://localhost:8000/admin`.

Run only one backend at a time. LiveKit sends calls to any registered worker, so a second one will silently take some of them, and because each keeps its own in-memory state those calls won't show up in the other's admin panel. The backend warns about this at startup.

### Without a microphone, or without keys

```bash
python backend/db/seed_demo_calls.py
python eval/run_eval.py --label demo
```

That populates `/admin` with calls, transcripts, traces and error-class badges, so you can look around without making a call.

To run the tests, `.env` has to exist, but the values can be anything. Nothing in the suite calls a live API.

```bash
pytest backend/tests eval/tests
```

516 tests, including all seven canonical scenarios driven through the real dispatcher, graph and database with Claude mocked. Every commit is gated on 512 of them, plus a secrets scan and two architecture checks.

There's also a hosted copy at **https://jupus-5661c.web.app** behind a shared access token, if you'd rather not set anything up. It's a single Railway instance, and it can't serve calls at the same time as a local backend for the reason above.

## How it works

```
browser ──WebRTC──▶ LiveKit room ──▶ agent worker (in the backend process)
                                            │
                                            ▼
                                    OpenAI Realtime
                              speech in, speech out, turn-taking
                                            │
                        one tool, ever: ask_supervisor(reason, utterance)
                                            ▼
                                 LangGraph supervisor (Claude)
                                            │
      greeting → routing → capture → research → booking → escalation
                                            │
                                            ▼
                          SQLite: calendar, call log, decision trace
```

Two models with separate jobs. OpenAI Realtime does everything about sounding like a conversation. A Claude state machine does everything about deciding what to do. Realtime never makes a business decision on its own; it has exactly one tool, and that tool asks the supervisor.

The rules that keep it that way:

- Realtime is never given a second tool. All business logic sits behind `ask_supervisor`.
- Every edge in the graph is a plain `if`/`else` on call state. A node never asks a model which node runs next.
- `validate_email` and `validate_phone` are regex, never a model call.
- Every tool call goes through one tracing wrapper, and every Claude call through one API wrapper. That's what makes the trace complete by construction rather than by discipline.
- No SQL outside `backend/db/repositories/`. Moving to Postgres means writing one set of classes and touching nothing else.

Two of these are checked by pre-commit hooks. The rest were reviewed by an independent agent before each merge.

## The four questions

### 1. Keeping latency low

I measured before optimising. Each turn is split into four stages, written as trace events, so the admin panel shows p50 and p95 from real calls.

The speech layer wasn't the problem: Realtime's listening is about 800ms and time to first audio about 750ms, against a supervisor round trip of 2.5s median and 5.6s p95. Three things took roughly 12% off it: merging the field extraction and the confirm-back into one Claude call, fixing a field that over-ran `max_tokens` and forced a silent retry, and moving two closed-set tools to Haiku. Prompt caching did nothing, because the prompts sit under Anthropic's 1024-token minimum.

After that you hit a floor, so the question becomes how long you wait *in silence*. Most turns hide the wait behind a question the agent needed anyway: it asks for your email while your name is still verifying in the background. The three turns with no cover get a short pre-recorded line. Time to first audio there went from 1796ms to 422ms. The round trip itself didn't move, and I'm not claiming it did.

### 2. Turn-taking, interruptions and noisy audio

I don't own endpointing, on purpose. Realtime's `semantic_vad` decides you've finished on whether your sentence sounds complete rather than on a silence timer, so "ummm" is a pause rather than an interruption. `eagerness: low` and near-field noise reduction came out of real calls where background noise was being read as speech.

Barge-in is on. The case that needed a decision is talking over the filler: "mhm" would reroute the turn for nothing, "actually it's Alex with an X" has to get through. That check is a closed token list rather than a model call, which would cost exactly the time the filler exists to hide.

The noisy-line handling matters more than the endpointing. Realtime writes the tool arguments itself, including what it thinks you said, and it invents: "manos44" reached the graph as `manos44@example.com`. So the real transcript takes precedence, finals only. Email and phone are always read back, and below 0.75 confidence it asks you to spell it out. Not solved: correct yourself mid-turn and the stale reply still plays first.

### 3. Making it good, and knowing whether it stays good

A transcript won't tell you whether a voice agent works, so this is where a lot of the effort went.

Every call is classified by an LLM judge against an editable taxonomy in `eval/error_classes.py`, and the judge has to cite the trace evidence, which makes a flag reviewable rather than an opinion. Since a judge grading its own system is a closed loop, one person annotates at `/admin/annotate` and is the only one who can change the taxonomy, and `calibrate_judge.py` scores the judge against those annotations.

For iteration, `replay_scenarios.py` runs the canonical scenarios through the real pipeline under a label and `compare_runs.py` diffs two labels, exiting non-zero on a regression. Every prompt change went through it. For scale, a concurrency test rather than an assumption: clean to 10 concurrent calls with no state leakage, checked per call. Past 10 it degrades, because SQLite has one writer and the default asyncio thread pool caps out; the second is a one-line fix. Live, I'd watch the latency p95 and the error-class rate.

### 4. Telephony and warm transfer

I didn't build this, so it's a design answer.

A SIP trunk into the same architecture, with only `backend/transport/` changing. The transport has already been swapped once, from a hand-rolled WebSocket to LiveKit, without the graph or the tools changing. One thing I wouldn't wave away: phone audio is 8kHz G.711 rather than Opus, worse input to the endpointing above, and those settings were tuned on browser audio.

On escalation the backend dials a second leg, briefs the human, then bridges the caller in. `generate_call_summary` already produces that summary for the handoff note; the transfer would speak it instead.

Failures land in two layers, each blind to some of the other's. Signalling: a 486 busy, a no-answer timer expiring, a 480 or 503 meaning the trunk is down (an operational alert, not just something to tell the caller), a BYE mid-bridge. Application: my own timeout on the bridge, and the one that matters most, the human picking up and saying nothing. SIP reports a healthy 200 OK while the caller is handed to nobody, so it's only catchable by listening for speech on their leg.

Busy or no answer, the caller is told and we take a callback, falling back to the handoff note that already gets written. Dropped mid-bridge, the agent comes back rather than disappearing, with a cap on retries. Never leave the caller in silence, and never let a failure at one layer be invisible at the other.

## The admin panel

At `http://localhost:8000/admin`:

- The call list, with outcome and error-class badges.
- Drill into any call for the full transcript and the complete decision trace: every tool call, retry, confidence score and stage transition, in order.
- Latency and cost: p50/p95 across the four stages, plus real per-call cost across both vendors from actual token counts.
- `/admin/escalations.html`, the handoff queue. Every escalated call, newest first, with why they rang, whatever details were confirmed, and why the agent gave up.
- `/admin/graph.html`, which shows a call moving through the graph live.
- `/admin/stress-test.html` for the concurrency test, and a raw table viewer.

## The eval commands

| Command | What it does |
|---|---|
| `python eval/run_eval.py --label <name>` | Stats plus the LLM judge over every logged call |
| `python eval/replay_scenarios.py --label <name>` | The canonical scenarios through the real, unmocked pipeline |
| `python eval/compare_runs.py --baseline a --candidate b` | Diffs error rates between two labelled runs |
| `python eval/calibrate_judge.py` | Scores the judge against the human annotations |
| `python eval/livekit_live_call.py --all` | Real LiveKit calls with synthesised speech, no human at a mic |
| `python eval/filler_latency_report.py` | The perceived-latency numbers in Q1 |
| `python eval/concurrency_stress_test.py` | N concurrent calls, checked for state leakage |

## What's where

| Path | |
|---|---|
| [`backend/transport/`](backend/transport/) | The LiveKit agent, the Realtime session config, the one tool schema, the filler audio |
| [`backend/dispatcher.py`](backend/dispatcher.py) | Supervisor dispatch, background verification and search, disconnect cleanup |
| [`backend/supervisor/`](backend/supervisor/) | The state machine: state, nodes and edges, tools, prompts, heuristics, tracing |
| [`backend/db/`](backend/db/) | Schema, seed scripts, and the repository layer, which is the only place SQL lives |
| [`backend/app.py`](backend/app.py) | FastAPI: room tokens, admin API, live trace stream |
| [`client/`](client/) | The caller's page |
| [`admin/`](admin/) | Call list, trace drill-in, handoff queue, live graph, annotation queue, stress test |
| [`eval/`](eval/) | The taxonomy, the judge, and the commands above |
| [`docs/reference/`](docs/reference/README.md) | A developer handbook written from the code: the life of a call, every node and state field, the schema, the API, the trace events |
| [`docs/fixes/`](docs/fixes/) | A numbered log of every non-trivial bug and its root cause |

[`docs/answers.md`](docs/answers.md) has longer versions of the four answers above, with the full measurement history.

## Known limits

- No telephony. Browser only. The design is in Q4.
- LiveKit is a third external dependency alongside OpenAI and Anthropic. Free, but it has to exist before any call will connect. I considered self-hosting and decided against it; a single-region SFU would probably make media latency worse.
- The agent worker runs inside the backend process. That's what lets it read call state directly and keeps the live admin view working, but it means one process is doing real-time media alongside serving HTTP.
- Call state is in memory, which is fine for one process and not for horizontal scaling.
- SQLite, not a hosted database. The repository layer is there so that's a contained change.
- The hosted deployment is one instance with no autoscaling, and its access token deters casual discovery rather than being real auth. The local setup is the path that always works.
- No cap on call duration, and the browser client has no automated tests.
- A call costs around $0.50, roughly 96% of it OpenAI Realtime, and most of that is conversation history being resent every turn. That's the first thing I'd attack if cost mattered.
