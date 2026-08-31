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

I measured before optimising. Every turn is split into four stages and each boundary is written as a trace event, so the admin panel shows p50 and p95 from real calls.

The speech layer turned out not to be the problem. Realtime's own listening and transcription is about 800ms, and time to first audio about 750ms. The supervisor round trip was 2.5 seconds at the median and 5.6 at p95. So everything after that was aimed at the supervisor.

Three things helped. Extracting a field and generating the confirm-back used to be two sequential Claude calls, and merging them into one took about 20% off that turn. A field was occasionally over-running `max_tokens`, truncating its JSON and forcing a silent retry, which is where the ten-second turns were coming from. And two tools that only pick from a closed set moved to Haiku. Net effect was about 12% off the round trip, measured by replaying the same scenarios before and after, with no regression in error rates.

One thing didn't help. I shipped prompt caching, measured it, and it does nothing here, because the prompts are under Anthropic's 1024-token minimum. The code is still there because it costs nothing if they grow.

Then you hit a floor. There is a real model call in the middle of the turn and no amount of transport work removes it. So the last piece changes the question from how long you wait to how long you wait in silence. Most turns already hide the wait behind a question the agent needed to ask anyway: it asks for your email while your name is still being verified in the background. Three turns have no cover, because you've just answered and the reply is the thing you're waiting for. Those get a short pre-recorded line in the same voice, and it only fires once the line has been quiet for a moment, so a fast turn is never narrated and it can't talk over you.

On those turns, time to first audio went from 1796ms to 422ms. The round trip itself didn't move, and I'm not claiming it did. It's the silence that got shorter.

### 2. Turn-taking, interruptions and noisy audio

I don't own endpointing, on purpose. Realtime's `semantic_vad` decides you've finished based on whether your sentence sounds complete, rather than on a silence timer. That's the difference between "ummm" being a pause and "ummm" cutting you off. I looked at third-party turn detection and didn't use it, because those want the raw audio stream, which means switching Realtime's own turn handling off and rebuilding the chained pipeline I was avoiding.

Two settings came out of real calls rather than the docs: `eagerness: low`, and near-field noise reduction, both after background noise was being picked up as speech.

Barge-in is on, so talking over the agent stops it. The case that needed an actual decision is talking over the filler. If it says "okay, one sec" and you say "mhm", that's acknowledgement, and feeding it to the graph would reroute the turn for nothing. If you say "actually it's Alex with an X", that's a correction, and dropping it makes you repeat yourself. The check is a closed list of tokens rather than a model call, because a model call there would cost exactly the time the filler exists to hide.

The noisy-line handling matters more than the endpointing. Realtime writes the tool call arguments itself, including its account of what you said, and it invents: a caller said "manos44" and the graph received `manos44@example.com`. So the real transcript takes precedence over the model's version, and only final transcripts are used. Email and phone are always read back regardless of confidence, and anything below 0.75 gets a deterministic "could you spell that out" rather than a guess.

Not solved: if you correct yourself mid-turn, the now-stale reply still gets spoken before the correction lands. Fixing it means being able to cancel a turn once its input is superseded, which reaches into the graph rather than the transport.

### 3. Making it good, and knowing whether it stays good

A transcript won't tell you whether a voice agent is working, so this is where a lot of the effort went.

Every call is classified by an LLM judge against an error taxonomy that lives in `eval/error_classes.py` and is meant to be edited. Four classes at the moment: repetition, surfaced failure, premature escalation, unconfirmed action. The judge has to cite what in the trace made it say so, which is what makes a flag reviewable instead of an opinion.

An LLM grading its own system is a closed loop, so two things break it open. One person annotates calls at `/admin/annotate` and is the only one who can approve a change to the taxonomy; the judge can propose one but can't apply it. And `eval/calibrate_judge.py` scores the judge against those annotations, so its precision and recall are known per class rather than assumed.

For iteration, `eval/replay_scenarios.py` runs the canonical scenarios through the real pipeline under a label, and `eval/compare_runs.py` diffs the error rates between two labels and exits non-zero on a regression. Every prompt change went through that, including the latency work above.

For scale, I ran a concurrency test rather than assuming. It holds up cleanly to 10 concurrent calls with no cross-call state leakage, and that's checked by inspecting each call's final state rather than inferred from nothing crashing. Past 10 it degrades, for two reasons I already knew about: SQLite has a single writer, and the default asyncio thread pool caps out. The second is a one-line fix in production.

Live, the two numbers worth watching are the latency p95 and the error-class rate. A latency regression points at prompts or models. An error-class regression points at conversation quality. They're different problems.

### 4. Telephony and warm transfer

I didn't build this, so this is a design answer rather than a description of running code.

A SIP trunk into the same architecture. Only `backend/transport/` would change, which I'm reasonably confident about because the transport has already been swapped once, from a hand-rolled WebSocket to LiveKit, without the graph or the tools changing. One thing I wouldn't wave away: phone audio is 8kHz G.711 rather than Opus, which is meaningfully worse input to the endpointing above, and those settings were tuned against browser audio. I'd expect to retune them.

For the transfer itself, when the agent escalates the backend dials a second leg to the human and briefs them before bridging the caller in. The briefing already exists. `generate_call_summary` produces the summary that gets written into the handoff note today; the transfer would speak it instead of writing it.

Failures land in two layers, and each is blind to some of what the other sees.

At the signalling layer, a 486 means they're on another call, a no-answer timer expiring means it rang and nobody was there, a 480 or 503 means the endpoint or the trunk is down, and a BYE mid-bridge means the line dropped after connecting. The 503 case is an operational alert, not just something to tell the caller about.

At the application layer the failures are different. There's my own timeout on the bridge attempt. And the one that matters most: the human picks up and says nothing, because it's voicemail or a phone answered in a pocket. SIP reports a healthy 200 OK, so every signalling-level check says success while the caller is being handed to nobody. That's only catchable by listening for speech on the human's leg.

What the caller hears matters more than which code fired. Busy or no answer, they're told a person isn't available and we take a callback, which falls back to the handoff note that already gets written. Dropped mid-bridge, the agent comes back rather than disappearing: "it looks like we got disconnected, let me try again", with a cap on retries before it falls back to the callback.

Two rules behind all of it. Never leave the caller in silence. And never let a failure at one layer be invisible at the other, because "the human answered" and "the human is talking to the caller" are different claims, and only one of them is something SIP can tell you.

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
