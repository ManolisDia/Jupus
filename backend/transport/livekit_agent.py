"""Phase 14 — the LiveKit Agents transport.

Replaces the hand-rolled WebRTC bridge (client/app.js's RTCPeerConnection +
backend/app.py's POST /session and WS /bridge + dispatcher.py's
SPEAKING/DEFERRED/CONNECTIONS bookkeeping) while keeping OpenAI Realtime as the
speech model, via LiveKit's own openai.realtime plugin. Everything below the
bridge — graph.py, tools.py, state.py, the repositories — is untouched, which
is the point: this phase migrates transport, not behaviour.

The worker runs IN-PROCESS with FastAPI rather than as a separate `lk agent`
process, because the supervisor's per-call state (CALL_STATES, the per-call
asyncio locks, the Phase 7/8 background task registries) lives in module-level
globals that the admin trace stream also reads. Splitting the agent into its
own process would have meant reintroducing an IPC bridge — exactly the thing
being removed — and would have broken the admin panel's live view.

Two facts about LiveKit's job model make that work, both verified against
livekit-agents 1.7.0 source rather than assumed:

1. Jobs run in a separate SUBPROCESS by default on Linux/macOS and a THREAD on
   Windows (worker.py's platform check). `job_executor_type` is therefore
   passed EXPLICITLY below — relying on the default would work on this Windows
   dev box and silently break the Railway deployment, where the agent would
   mutate a CALL_STATES in the wrong process.
2. A THREAD job still gets its OWN event loop (ipc/proc_client.py calls
   asyncio.new_event_loop()). Module globals are genuinely shared, but asyncio
   primitives are not portable across loops — an asyncio.Lock binds to the
   first loop that awaits it and raises afterwards if awaited from another.
   dispatcher.get_lock() hands out exactly such locks, so every supervisor call
   is marshalled back onto the FastAPI loop by _on_main_loop() below. That
   keeps the concurrency model identical to the pre-Phase-14 one instead of
   introducing a second, subtly different one.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import suppress
from pathlib import Path
from typing import Any, Awaitable, Optional, TypeVar

from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    JobContext,
    JobExecutorType,
    RunContext,
    StopResponse,
    function_tool,
)
from livekit.agents.utils.audio import audio_frames_from_file
from livekit.plugins import openai
from openai.types.realtime import AudioTranscription
from openai.types.realtime.realtime_audio_input_turn_detection import SemanticVad

from backend.config import settings
from backend.db.repositories import Repositories
from backend.dispatcher import call_state_snapshot, mark_call_abandoned, run_supervisor_turn
from backend.supervisor.fillers import (
    FILLER_IDLE_DELAY_SECONDS,
    FILLER_PHRASES,
    FILLER_REPEAT_SECONDS,
    VOICE,
    VOICE_SPEED,
    filler_for_state,
)
from backend.supervisor.heuristics import looks_like_acknowledgment
from backend.supervisor.state import CALL_STATES
from backend.transport.prompts import ASK_SUPERVISOR_SCHEMA, SUPERVISOR_INSTRUCTIONS

logger = logging.getLogger(__name__)


def _handle_done(handle: Any) -> bool:
    done = getattr(handle, "done", None)
    return bool(done()) if callable(done) else False

FILLER_AUDIO_DIR = Path(__file__).resolve().parent / "filler_audio"

# Mirrors the pre-Phase-14 client/app.js session.update: same model, voice,
# noise reduction, transcription model, and semantic_vad eagerness.
# docs/DECISIONS.md records live-testing reasons for several of these
# (eagerness "low" plus near_field to stop background noise being read as
# speech; interrupt_response true for real barge-in).
#
# One deliberate difference: output speed is 1.1x where the old config used
# 1.5x — a separate, explicitly-requested voice change (commit 0bd2892), not a
# side effect of the migration. Everything else is held constant precisely so a
# live regression stays attributable.
REALTIME_MODEL = "gpt-realtime-2.1"
# Pinned, not left to the plugin's default (gpt-4o-mini-transcribe). This is
# the model the pre-Phase-14 session config used, and transcription fidelity is
# load-bearing here: it is what _verbatim_utterance trusts INSTEAD of the
# Realtime model's own invented tool argument.
TRANSCRIPTION_MODEL = "gpt-transcribe"

# How long a turn waits for the caller's real transcript before falling back to
# the model's argument. Generous enough to win the usual race, short enough
# that it never becomes the thing the caller is waiting on.
TRANSCRIPT_WAIT_SECONDS = 1.5

# Data-channel topic for the caller client's "captured details" panel.
# Topic-scoped so a future data topic can't be misread as call state.
CALL_STATE_TOPIC = "jupus.call_state"

T = TypeVar("T")

# The FastAPI event loop, captured at startup. Every asyncio primitive the
# supervisor touches is bound to it — see this module's docstring.
MAIN_LOOP: Optional[asyncio.AbstractEventLoop] = None
_REPOS: Optional[Repositories] = None


async def _on_main_loop(coro: Awaitable[T]) -> T:
    """Await `coro` on the FastAPI loop, from whichever loop is calling.

    A no-op passthrough when already on that loop (tests, and any future
    single-loop deployment), so the bridge never costs anything it doesn't
    have to and test code doesn't need a running worker to exercise this path.
    """
    if MAIN_LOOP is None or MAIN_LOOP is asyncio.get_running_loop():
        return await coro
    return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(coro, MAIN_LOOP))


class JupusAgent(Agent):
    """The caller-facing agent: one tool, one call.

    A fresh instance per job, so per-call bookkeeping lives safely on `self`
    rather than in another module-level dict keyed by call_id.
    """

    def __init__(self, call_id: str, repos: Repositories, room: Any = None) -> None:
        super().__init__(instructions=SUPERVISOR_INSTRUCTIONS)
        self._call_id = call_id
        self._repos = repos
        # Optional so unit tests can build an agent without a live room; with
        # no room there is simply nothing to publish call state to.
        self._room = room
        # Filler speech handles created for the turn currently in flight. Used
        # only to answer "did the caller cut off a filler?" — see ask_supervisor.
        self._filler_handles: list[Any] = []
        # The caller's last FINAL ASR transcript, waiting to be claimed by the
        # turn it belongs to. See _verbatim_utterance for why this exists.
        self._transcript: Optional[str] = None
        self._transcript_ready = asyncio.Event()
        # Two DIFFERENT clocks, both cleared by the first agent audio.
        # _turn_started_at answers Phase 14's question (how long was the caller
        # in silence, filler or not); _reply_ready_at answers Phase 11's
        # (how long from the supervisor answering to the reply being audible).
        # Collapsing them into one would double-count supervisor time inside
        # total_perceived.
        self._turn_started_at: Optional[tuple[str, float]] = None
        self._reply_ready_at: Optional[tuple[str, float]] = None

    # -- verbatim transcript (docs/DECISIONS.md) ---------------------------

    def note_transcript(self, transcript: str) -> None:
        """Record a final ASR transcript for the next turn to claim."""
        self._transcript = transcript
        self._transcript_ready.set()

    def _claim_transcript(self) -> Optional[str]:
        transcript, self._transcript = self._transcript, None
        self._transcript_ready.clear()
        return transcript

    async def _verbatim_utterance(self, raw_arguments: dict[str, object]) -> str:
        """What the caller ACTUALLY said, preferred over what the model says
        they said.

        `last_caller_utterance` is not a passthrough of speech recognition — it
        is a string the Realtime model generates when building the tool call,
        and it invents. docs/DECISIONS.md records the live case: a caller said
        "manos44" and `extract_field` received `manos44@example.com`, the model
        having added an `@` and a domain that were never spoken. That silently
        repairs exactly the malformed input the confidence/validation pipeline
        exists to catch. That entry also records that tightening the prompt was
        tried and was NOT reliable, which is why the tool schema's stern
        wording is not on its own considered a fix.

        The pre-Phase-14 client solved it as `lastVerbatimTranscript ??
        args.last_caller_utterance`. This is that same precedence, rebuilt on
        LiveKit's `user_input_transcribed` event.

        The short wait matters as much as the precedence. ASR completion and
        the model's tool call race, and the tool call can win — the old client
        hit exactly this and it produced a call stuck re-asking one turn behind
        (docs/fixes/2026-08-25-001.md). Waiting briefly for the real transcript
        is better than trusting an invented one; falling back to the model's
        argument after the timeout is better than dropping the turn.
        """
        model_argument = str(raw_arguments.get("last_caller_utterance") or "")
        if not self._transcript_ready.is_set():
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(
                    self._transcript_ready.wait(), timeout=TRANSCRIPT_WAIT_SECONDS
                )
        return self._claim_transcript() or model_argument

    # -- latency boundaries (Phase 11) -------------------------------------

    def note_caller_stopped_speaking(self) -> None:
        """The `speech_stopped` boundary, which lost its producer with /bridge.

        It used to be reported by client/app.js off Realtime's VAD events. It
        opens the stt_and_dialogue_decision stage — without it that stage reads
        as no-data for every call, which is how Phase 11's latency view
        silently went dark under the new transport.
        """
        self._repos.trace.record_event(self._call_id, "speech_stopped", node="transport")

    def note_agent_started_speaking(self) -> None:
        """The caller can now hear something. Closes both audio boundaries.

        This is a real playout signal (LiveKit's agent state entering
        "speaking"), which is strictly better than what it replaces — the old
        client inferred it from the remote audio track's amplitude, because
        WebRTC never surfaced per-chunk audio events.

        `first_audio` fires for whichever comes first, filler or real reply,
        because from the caller's side that IS when the silence ends. It is the
        honest measure of what Phase 14 changed, unlike timing the moment say()
        was *called* — which excludes the speech queue and playout entirely,
        and is how an earlier version of this phase reported ~400ms for a clip
        that took 1.3s to make a sound.

        `tts_first_audio` keeps Phase 11's narrower meaning (supervisor
        answered -> reply audible) so total_perceived's arithmetic stays valid.
        """
        now = time.monotonic()
        if self._turn_started_at is not None:
            tool_call_id, started = self._turn_started_at
            self._turn_started_at = None
            self._repos.trace.record_event(
                self._call_id, "first_audio", node="transport",
                tool_call_id=tool_call_id, ms_since_turn_start=int((now - started) * 1000),
            )
        if self._reply_ready_at is not None:
            tool_call_id, ready = self._reply_ready_at
            self._reply_ready_at = None
            self._repos.trace.record_event(
                self._call_id, "tts_first_audio", node="transport",
                tool_call_id=tool_call_id, ms_since_reply_delivered=int((now - ready) * 1000),
            )

    # -- filler ------------------------------------------------------------

    def _say_filler(self, session: AgentSession, key: str, step: int, played: list) -> Any:
        """Play filler line `step` for `key`, or None when there are no more.

        Returning a SpeechHandle (rather than a str) is what lets this work at
        all: session.say() refuses text-only input under a RealtimeModel
        because the OpenAI plugin reports supports_say=False, but the audio=
        branch bypasses that guard. The clips are pre-rendered in the same
        voice and speed as the live session (fillers.py), so the seam is
        inaudible.
        """
        lines = FILLER_PHRASES[key]
        if step >= len(lines):
            return None
        path = FILLER_AUDIO_DIR / f"{key}_{step}.wav"
        handle = session.say(
            lines[step],
            audio=audio_frames_from_file(str(path)),
            allow_interruptions=True,
            # Kept out of the Realtime chat context deliberately. The filler is
            # a transport-level UX device, not part of the conversation the
            # supervisor reasons about; feeding it back to the model would give
            # it a new way to think it had already answered. The transcript
            # that matters is CallState["transcript"], maintained by the graph.
            add_to_chat_ctx=False,
        )
        played.append(handle)
        # Shared across overlapping turns purely so the interrupt guard can see
        # a filler that a DIFFERENT in-flight turn started. Pruned of finished,
        # uninterrupted handles so it can't grow for the life of the call.
        self._filler_handles = [
            h for h in self._filler_handles if h.interrupted or not _handle_done(h)
        ]
        self._filler_handles.append(handle)
        self._repos.trace.record_event(
            self._call_id, "filler_spoken", node="transport", filler=key, step=step
        )
        return handle

    def _consume_filler_interruption(self) -> bool:
        """Did the caller cut off a filler that hasn't been acted on yet?

        Consuming (rather than just reading) is what makes this safe against
        overlapping turns. Two ask_supervisor calls can be in flight at once
        for the same call — LiveKit keeps a tool running after its speech is
        interrupted, and the interrupting utterance starts its own turn — so
        this state is genuinely shared, and an earlier version simply reset the
        list at the top of every turn. That lost the evidence: a substantive
        interruption cleared the list while the first turn's filler was still
        the thing that had been cut off, and a backchannel arriving right after
        it sailed past the guard into the graph. Reproduced before fixing.

        Clearing exactly the handles that were interrupted keeps the guard
        armed for as long as an unhandled interruption exists, and disarms it
        the moment one turn takes responsibility for it — whether by
        suppressing a backchannel or by processing a real correction.
        """
        interrupted = [handle for handle in self._filler_handles if handle.interrupted]
        if not interrupted:
            return False
        self._filler_handles = [h for h in self._filler_handles if not h.interrupted]
        return True

    # -- the one tool (CLAUDE.md rule #1) ----------------------------------

    @function_tool(raw_schema=ASK_SUPERVISOR_SCHEMA)
    async def ask_supervisor(self, ctx: RunContext, raw_arguments: dict[str, object]) -> str:
        # What the caller actually said, not what the model says they said.
        utterance = await self._verbatim_utterance(raw_arguments)

        # Phase 11's stage boundary, kept under the new transport: recorded
        # before any work so the timestamp reflects actual receipt. Paired with
        # reply_ready below, these two bracket the supervisor round trip, and
        # the gap between this and filler_spoken is what the caller actually
        # experiences as the wait. Both are needed for Phase 14's DoD to show
        # perceived latency and round-trip latency side by side rather than
        # asserting the distinction.
        self._repos.trace.record_event(
            self._call_id,
            "ask_supervisor_received",
            node="transport",
            tool_call_id=ctx.function_call.call_id,
        )
        self._turn_started_at = (ctx.function_call.call_id, time.monotonic())

        # Decision 3: a caller talking over the filler purely to acknowledge it
        # ("mhm", "okay") must not become a turn of its own. Without this guard
        # every backchannel would reach the graph as a real utterance and
        # reroute the conversation. A substantive interruption deliberately
        # falls through — it becomes the next turn's input, serialized behind
        # the in-flight turn by dispatcher.get_lock(), so it reaches the graph
        # rather than being discarded.
        if self._consume_filler_interruption() and looks_like_acknowledgment(utterance):
            self._repos.trace.record_event(
                self._call_id,
                "filler_interruption_ignored",
                node="transport",
                utterance=utterance,
            )
            raise StopResponse()

        state = CALL_STATES.get(self._call_id)
        filler_key = filler_for_state(state) if state else None

        # Per-TURN, unlike self._filler_handles: what this turn played is not
        # what some overlapping turn played, and reply_ready reports on this
        # turn. No reset of the shared list here — another turn may still be
        # inside its own with_filler block, and wiping its handles from under
        # it is exactly the bug _consume_filler_interruption documents.
        played: list[Any] = []
        if filler_key is None:
            reply, _stage = await self._run_turn(ctx, utterance, played)
            return reply

        # The scheduler only fires once the session has been continuously idle
        # for FILLER_IDLE_DELAY_SECONDS, so it never talks over the caller and
        # a fast turn is never narrated at all. If the supervisor is still
        # working FILLER_REPEAT_SECONDS later, line [1] follows — see
        # fillers.py on why a single line would reproduce the Phase 2 finding.
        async with ctx.with_filler(
            lambda step: self._say_filler(ctx.session, filler_key, step, played),
            delay=FILLER_IDLE_DELAY_SECONDS,
            interval=FILLER_REPEAT_SECONDS,
            max_steps=len(FILLER_PHRASES[filler_key]),
        ):
            reply, _stage = await self._run_turn(ctx, utterance, played)
        return reply

    async def _run_turn(self, ctx: RunContext, utterance: str, played: list) -> tuple[str, str]:
        # Decision 4: run_supervisor_turn never raises — an upstream failure
        # comes back as _llm_failure_fallback's graceful reply and its
        # 3-strikes escalation bookkeeping (CLAUDE.md rule #7). The filler
        # having already played changes nothing about that contract; it just
        # means the fallback is spoken one beat later.
        result = await _on_main_loop(
            run_supervisor_turn(
                self._repos, self._call_id, ctx.function_call.call_id, utterance
            )
        )
        reply, dispatch_stage = result
        # The round-trip end boundary. Under the /bridge transport this was
        # reply_delivered, emitted by deliver_or_defer once it decided the
        # caller wasn't mid-sentence; here LiveKit's own turn-taking makes that
        # decision, so the honest thing to record is simply "the supervisor
        # answered", with no deferral bookkeeping attached to it.
        self._repos.trace.record_event(
            self._call_id,
            "reply_ready",
            node="transport",
            tool_call_id=ctx.function_call.call_id,
            reply=reply,
            dispatch_stage=dispatch_stage,
            filler_played=bool(played),
        )
        self._reply_ready_at = (ctx.function_call.call_id, time.monotonic())
        await self._publish_call_state()
        return result

    async def _publish_call_state(self) -> None:
        """Push the caller-profile snapshot to the browser's "captured details"
        panel — the LiveKit data-channel replacement for dispatcher's
        broadcast_call_state over the /bridge WebSocket.

        Display-only and strictly one-way, exactly as before: nothing the
        client does with this can reach back into the call. Failures are
        swallowed because a cosmetic panel update must never be able to break
        a live call.
        """
        if self._room is None:
            return
        state = CALL_STATES.get(self._call_id)
        if state is None:
            return
        try:
            await self._room.local_participant.publish_data(
                json.dumps(call_state_snapshot(state)),
                topic=CALL_STATE_TOPIC,
                reliable=True,
            )
        except Exception:
            logger.warning("failed to publish call_state for %s", self._call_id, exc_info=True)


def build_session() -> AgentSession:
    return AgentSession(
        llm=openai.realtime.RealtimeModel(
            model=REALTIME_MODEL,
            voice=VOICE,
            speed=VOICE_SPEED,
            api_key=settings.openai_api_key,
            input_audio_noise_reduction="near_field",
            input_audio_transcription=AudioTranscription(model=TRANSCRIPTION_MODEL),
            turn_detection=SemanticVad(
                type="semantic_vad",
                eagerness="low",
                create_response=True,
                interrupt_response=True,
            ),
        )
    )


async def entrypoint(ctx: JobContext) -> None:
    """One job per call. The room name IS the call_id — the browser mints its
    token against a room named after the call it just created, so the id
    travels end to end without a side channel.

    Read from ctx.job.room.name rather than ctx.room.name: the latter's
    JobContext docstring warns that some Room properties are unpopulated before
    connect(), and this is needed before then.
    """
    call_id = ctx.job.room.name
    ctx.log_context_fields = {"call_id": call_id}
    assert _REPOS is not None, "start_agent_server() must run before any job"
    repos = _REPOS

    await ctx.connect()
    session = build_session()
    agent = JupusAgent(call_id, repos, ctx.room)

    @session.on("user_state_changed")
    def _on_user_state(event) -> None:  # noqa: ANN001 — LiveKit event type
        if getattr(event, "new_state", None) == "listening":
            agent.note_caller_stopped_speaking()

    @session.on("agent_state_changed")
    def _on_agent_state(event) -> None:  # noqa: ANN001 — LiveKit event type
        if getattr(event, "new_state", None) == "speaking":
            agent.note_agent_started_speaking()

    @session.on("user_input_transcribed")
    def _on_transcript(event) -> None:  # noqa: ANN001 — LiveKit event type
        # Only finals: interim transcripts are exactly the half-heard text the
        # model would otherwise be inventing around.
        if getattr(event, "is_final", False) and event.transcript:
            agent.note_transcript(event.transcript)

    # Phase 11's cost accounting covers BOTH vendors, and the OpenAI Realtime
    # half used to arrive as client-reported `realtime_usage` bridge messages
    # (client/app.js read them off response.done). With /bridge gone that half
    # would silently read $0 forever, which is worse than not tracking it at
    # all. LiveKit reports the same token counts itself, so they're captured
    # here instead and written in the exact payload shape
    # eval/insights_agent.py's _cost_for_call already sums.
    #
    # session_usage_updated carries CUMULATIVE session totals, not per-response
    # deltas, so the latest value is stashed and emitted once at shutdown —
    # recording every update would multiply the real cost by the number of
    # turns.
    latest_usage: dict[str, int] = {}

    @session.on("session_usage_updated")
    def _on_usage(event) -> None:  # noqa: ANN001 — LiveKit event type
        # model_usage is keyed per (provider, model), so SUM across entries
        # rather than overwriting — with one model today that's the same
        # number, but silently discarding a second model's cost later would
        # be exactly the kind of quiet under-reporting this event exists to
        # prevent. Cached audio/text tokens are folded into their uncached
        # counterparts because pricing.estimate_cost_usd has no cached-audio
        # rate; counting them is closer to the truth than dropping them.
        totals = {k: 0 for k in
                  ("input_audio_tokens", "output_audio_tokens",
                   "input_text_tokens", "output_text_tokens")}
        for usage in getattr(event.usage, "model_usage", []):
            if getattr(usage, "type", None) != "llm_usage":
                continue
            totals["input_audio_tokens"] += (
                getattr(usage, "input_audio_tokens", 0)
                + getattr(usage, "input_cached_audio_tokens", 0)
            )
            totals["output_audio_tokens"] += getattr(usage, "output_audio_tokens", 0)
            totals["input_text_tokens"] += (
                getattr(usage, "input_text_tokens", 0)
                + getattr(usage, "input_cached_text_tokens", 0)
            )
            totals["output_text_tokens"] += getattr(usage, "output_text_tokens", 0)
        latest_usage.clear()
        latest_usage.update(totals)

    async def _on_shutdown() -> None:
        if latest_usage:
            repos.trace.record_event(
                call_id, "realtime_usage", node="transport", **latest_usage
            )
        else:
            # Loud on purpose: silently recording nothing here is exactly the
            # "$0 forever" outcome this handler exists to prevent, and it would
            # look identical to a genuinely free call.
            logger.warning(
                "no Realtime usage captured for call_id=%s — its OpenAI cost "
                "will read as zero in the eval/admin views",
                call_id,
            )
        # The disconnect-cleanup contract from docs/phases/cross-cutting.md
        # section 2, moved off the retired /bridge WebSocketDisconnect handler.
        # Marshalled onto the FastAPI loop because it takes the same per-call
        # lock a turn does.
        await _on_main_loop(mark_call_abandoned(repos, call_id))

    ctx.add_shutdown_callback(_on_shutdown)

    await session.start(agent=agent, room=ctx.room)


def build_server() -> AgentServer:
    server = AgentServer(
        job_executor_type=JobExecutorType.THREAD,
        num_idle_processes=0,
        # Bind the worker's health endpoint to a random loopback port so it
        # can't collide with uvicorn's (its prod default is a fixed 8081, on
        # all interfaces).
        host="127.0.0.1",
        port=0,
        ws_url=settings.livekit_url,
        api_key=settings.livekit_api_key,
        api_secret=settings.livekit_api_secret,
    )
    # No agent_name: setting one switches LiveKit to EXPLICIT dispatch, and the
    # agent would then never be dispatched to a room unless the token or the
    # dispatch API asked for it by name. Automatic dispatch is what this
    # single-agent project wants — every room created is a call.
    server.rtc_session(entrypoint)
    return server


_server: Optional[AgentServer] = None
_server_task: Optional[asyncio.Task] = None


def start_agent_server(repos: Repositories) -> None:
    """Launch the worker as a background task on the current (FastAPI) loop.

    A no-op (with a warning) when LiveKit isn't configured. Without that guard
    the worker raises ValueError("ws_url is required") inside a bare
    create_task, where nothing awaits it — the backend appears to boot fine,
    /livekit-token returns a helpful 503, and the agent is simply dead with no
    message anywhere.
    """
    global MAIN_LOOP, _REPOS, _server, _server_task
    if not (settings.livekit_url and settings.livekit_api_key and settings.livekit_api_secret):
        logger.warning(
            "LiveKit is not configured (LIVEKIT_URL/API_KEY/API_SECRET) — the agent worker "
            "will NOT start and no call can connect. Admin and eval endpoints still work."
        )
        return
    # Automatic dispatch means EVERY worker registered against this LiveKit
    # project is a candidate for every call. A second backend left running with
    # JUPUS_TRANSPORT=livekit — a stale terminal, a forgotten --reload — will
    # happily take calls this one expects to handle, and because that other
    # process has its own CALL_STATES, the call simply vanishes from this one's
    # admin panel and database with no error anywhere. This cost real debugging
    # time during Phase 14; the warning is here so it costs nobody else any.
    logger.warning(
        "LiveKit worker registering with automatic dispatch — it will accept calls for ANY "
        "room in this project. Make sure no other backend is running with "
        "JUPUS_TRANSPORT=livekit, or calls will be split between them at random."
    )
    MAIN_LOOP = asyncio.get_running_loop()
    _REPOS = repos
    _server = build_server()
    # run() blocks until the worker closes, so it has to be a task rather than
    # an await — it registers outbound with LiveKit Cloud and then serves jobs
    # for the process's lifetime.
    _server_task = asyncio.create_task(_server.run(), name="livekit_agent_server")

    def _log_if_it_dies(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        if exc := task.exception():
            logger.error("LiveKit agent worker stopped: %s", exc, exc_info=exc)

    # Without this the worker's exception sits on an un-awaited task and
    # surfaces, if at all, as an "exception was never retrieved" warning at GC.
    _server_task.add_done_callback(_log_if_it_dies)
    logger.info("LiveKit agent worker starting (url=%s)", settings.livekit_url)


async def stop_agent_server() -> None:
    global _server, _server_task
    if _server is None:
        return
    global MAIN_LOOP
    try:
        # Explicit timeout: drain_timeout defaults to a full hour, which would
        # hang shutdown on any still-connected call.
        await _server.drain(timeout=10)
        await _server.aclose()
    except Exception:  # noqa: BLE001 — shutdown is best-effort, and this runs
        # inside the lifespan's finally, where raising would mask whatever
        # actually caused the shutdown.
        logger.warning("LiveKit worker did not shut down cleanly", exc_info=True)
    if _server_task is not None:
        _server_task.cancel()
    # Cleared so a restarted lifespan can't marshal work onto a closed loop.
    _server, _server_task, MAIN_LOOP = None, None, None
