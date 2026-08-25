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

FILLER_AUDIO_DIR = Path(__file__).resolve().parent / "filler_audio"

# Mirrors the pre-Phase-14 client/app.js session.update exactly: same model,
# voice, speed, noise reduction, transcription model, and semantic_vad
# eagerness. docs/DECISIONS.md records live-testing reasons for several of
# these (eagerness "low" plus near_field to stop background noise being read as
# speech; interrupt_response true for real barge-in). Changing any of them in a
# transport migration would make a live regression impossible to attribute.
REALTIME_MODEL = "gpt-realtime-2.1"

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

    # -- filler ------------------------------------------------------------

    def _say_filler(self, session: AgentSession, key: str, step: int) -> Any:
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
        self._filler_handles.append(handle)
        self._repos.trace.record_event(
            self._call_id, "filler_spoken", node="transport", filler=key, step=step
        )
        return handle

    def _caller_cut_off_a_filler(self) -> bool:
        return any(handle.interrupted for handle in self._filler_handles)

    # -- the one tool (CLAUDE.md rule #1) ----------------------------------

    @function_tool(raw_schema=ASK_SUPERVISOR_SCHEMA)
    async def ask_supervisor(self, ctx: RunContext, raw_arguments: dict[str, object]) -> str:
        utterance = str(raw_arguments.get("last_caller_utterance") or "")

        # Decision 3: a caller talking over the filler purely to acknowledge it
        # ("mhm", "okay") must not become a turn of its own. Without this guard
        # every backchannel would reach the graph as a real utterance and
        # reroute the conversation. A substantive interruption deliberately
        # falls through — it becomes the next turn's input, serialized behind
        # the in-flight turn by dispatcher.get_lock(), so it reaches the graph
        # rather than being discarded.
        if self._caller_cut_off_a_filler() and looks_like_acknowledgment(utterance):
            self._repos.trace.record_event(
                self._call_id,
                "filler_interruption_ignored",
                node="transport",
                utterance=utterance,
            )
            raise StopResponse()

        state = CALL_STATES.get(self._call_id)
        filler_key = filler_for_state(state) if state else None

        self._filler_handles = []
        if filler_key is None:
            reply, _stage = await self._run_turn(ctx, utterance)
            return reply

        # The scheduler only fires once the session has been continuously idle
        # for FILLER_IDLE_DELAY_SECONDS, so it never talks over the caller and
        # a fast turn is never narrated at all. If the supervisor is still
        # working FILLER_REPEAT_SECONDS later, line [1] follows — see
        # fillers.py on why a single line would reproduce the Phase 2 finding.
        async with ctx.with_filler(
            lambda step: self._say_filler(ctx.session, filler_key, step),
            delay=FILLER_IDLE_DELAY_SECONDS,
            interval=FILLER_REPEAT_SECONDS,
            max_steps=len(FILLER_PHRASES[filler_key]),
        ):
            reply, _stage = await self._run_turn(ctx, utterance)
        return reply

    async def _run_turn(self, ctx: RunContext, utterance: str) -> tuple[str, str]:
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

    async def _on_shutdown() -> None:
        # The disconnect-cleanup contract from docs/phases/cross-cutting.md
        # section 2, moved off the retired /bridge WebSocketDisconnect handler.
        # Marshalled onto the FastAPI loop because it takes the same per-call
        # lock a turn does.
        await _on_main_loop(mark_call_abandoned(repos, call_id))

    ctx.add_shutdown_callback(_on_shutdown)

    await ctx.connect()
    session = build_session()
    await session.start(agent=JupusAgent(call_id, repos, ctx.room), room=ctx.room)


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
    """Launch the worker as a background task on the current (FastAPI) loop."""
    global MAIN_LOOP, _REPOS, _server, _server_task
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
    logger.info("LiveKit agent worker starting (url=%s)", settings.livekit_url)


async def stop_agent_server() -> None:
    global _server, _server_task
    if _server is None:
        return
    try:
        # Explicit timeout: drain_timeout defaults to a full hour, which would
        # hang shutdown on any still-connected call.
        await _server.drain(timeout=10)
    except (asyncio.TimeoutError, Exception):  # noqa: B014 — shutdown is best-effort
        logger.warning("LiveKit worker drain did not complete cleanly", exc_info=True)
    await _server.aclose()
    if _server_task is not None:
        _server_task.cancel()
    _server, _server_task = None, None
