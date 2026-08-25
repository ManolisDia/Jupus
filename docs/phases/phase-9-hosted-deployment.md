# Phase 9 — Hosted Deployment (Railway backend + Firebase Hosting client)

## Revision note
Numbered Phase 9 (originally scoped as Phase 13, before the intended build order was settled) — sequenced right after Phase 8 (legal research) and before Phase 10 (telephony), since Phase 10's Twilio webhooks need this phase's public URL as a hard prerequisite. See `docs/phases/phase-15-polish-submission.md`'s own renumbering note for the full history.

## Goal

Stand up a real, publicly reachable deployment of this project — backend on Railway (with persistent storage), caller-facing client on Firebase Hosting — so evaluation isn't limited to "clone and run locally." This is additive: the local path (`pip install`, `uvicorn`, open `client/index.html`) stays exactly as documented and remains the primary, always-works submission path (`docs/DECISIONS.md`'s existing Railway-stretch entry already establishes this framing; this phase is that entry made concrete). Hosting also becomes a hard dependency for Phase 10 (telephony) if that's built — Twilio's webhooks need a public URL regardless.

## Prerequisite

None structurally — can be done at any point once there's something worth deploying (i.e. after Phase 5 at the earliest). Sequenced as Phase 9 (right after Phase 8, before Phase 10) because Phase 10 (telephony) needs this phase's public URL as a hard prerequisite — do this one before attempting telephony. Read `docs/DECISIONS.md`'s existing "No telephony, no Docker, no Railway hosting in v1" entry before starting — it already states the constraints this phase has to satisfy (gated access, spend caps, local path stays primary).

## Why this exists

Came out of two separate, concrete conversations: deciding to actually deploy telephony (Phase 10) means Twilio *needs* a public webhook URL, no way around it — and separately, hosting the WebRTC/browser path too means an evaluator can try the live agent from a link without any local setup at all, which is a strictly better experience than asking them to clone a repo for a first impression. The SQLite-vs-Postgres question that came up while scoping this was resolved in `docs/DECISIONS.md` terms already: Volume-mounted SQLite, not a Postgres migration — see Decision 1 below for why, restated here.

## Non-goals

- **Not migrating off SQLite.** Decided already: a Railway Volume mounted at `settings.db_path` solves the actual problem (persistence across redeploys) with zero code changes. Postgres would only pay off once `CALL_STATES`/`LOCKS`/`SPEAKING`/`DEFERRED`/`CONNECTIONS` are also externalized for horizontal scaling — this phase deliberately keeps the backend single-instance, so that payoff never materializes here. `backend/db/repositories/__init__.py`'s `NotImplementedError` for `db_backend != "sqlite"` stays exactly as-is.
- **Not enabling autoscaling / multiple Railway replicas.** The in-memory dispatcher state is a hard single-instance constraint (already documented in the README's "Known limits"). This phase makes that constraint *load-bearing* rather than theoretical — Railway must be configured for exactly one running instance.
- **Not building a real authentication system.** Access gating (Decision 3) is a single shared-secret check, not user accounts, not OAuth — consistent with this being a local-cost, single-annotator prototype everywhere else (`docs/benevolent_dictator.md`'s no-real-auth stance for `/admin` is the same category of decision).
- **Not moving the admin panel to Firebase.** `/admin` and `/admin/annotate` stay served by the backend itself (`app.mount("/admin", StaticFiles(...))`, unchanged) — only `client/index.html` (the caller-facing page) moves to Firebase Hosting. The admin panel has no reason to be public at all (Decision 3 covers whether it should even be reachable without the shared secret).
- **Not setting up a custom domain.** Railway's and Firebase's default generated domains (`*.up.railway.app`, `*.web.app`) are enough for an evaluation deployment; a custom domain is pure polish with zero functional benefit here.
- **Not building CI/CD.** Manual deploy (`railway up`, `firebase deploy`) is enough for a time-boxed evaluation window — this isn't a product that gets iterated on after the fact.

## Decisions made, not left open for the implementer

**1. SQLite on a Railway Volume, not Postgres — restated from the earlier design conversation.** `backend/db/repositories/__init__.py` already documents the seam (`db_backend: Literal["sqlite", "postgres"]`) but nothing implements the Postgres branch. A Volume mounted at the same path `JUPUS_DB_PATH` already points to means zero repository code changes — `connect(settings.db_path)` behaves identically whether that path is on ephemeral local disk (today) or a persistent Volume (after this phase). This is the whole fix.

**2. `client/app.js`'s two hardcoded constants become environment-driven, not hand-edited per deploy.** `BACKEND_URL = "http://localhost:8000"` and `BRIDGE_WS_URL = "ws://localhost:8000/bridge"` are currently literal strings at the top of the file. Since `client/index.html` is deliberately unbundled (no build step, opened directly — `docs/DECISIONS.md`), there's no environment-variable injection mechanism today. Simplest fix that doesn't introduce a build step: a small `client/config.js` (loaded before `app.js` via a `<script>` tag), git-ignored with a committed `client/config.example.js`, holding just `window.JUPUS_BACKEND_URL`/`window.JUPUS_BRIDGE_WS_URL` — `app.js` reads those instead of hardcoding. Local dev keeps working with a `config.js` pointing at `localhost:8000`; the Firebase-deployed `config.js` points at the Railway domain, using `wss://`, not `ws://` (mixed-content blocking — Decision 6 below).

**3. Public access is gated behind one shared-secret query param, checked on every route that costs money or touches state — not just `/session`.** A deployed `/session` endpoint that mints real ephemeral Realtime tokens on request is a direct spend/abuse vector the moment its URL is known. Gate: a `JUPUS_ACCESS_TOKEN` env var on the backend; `POST /session` and `WS /bridge` both require a matching `?access_token=` (or header, for `/session`'s POST) and reject with 401/close the socket if absent/wrong. The client's `config.js` (Decision 2) carries the token, embedded in the deployed page — not perfectly secret from a determined caller inspecting page source, but sufficient to stop the URL from being casually crawled/abused, which is the actual threat model for a time-boxed evaluation deployment, not a production security boundary. `/admin`/`/admin/annotate` get the same query-param gate (Decision non-goal above — not auth, just "don't leave it wide open to the public internet" given it's not meant to be a public-facing surface at all).

**4. Hard spend caps/alerts on OpenAI and Anthropic billing are a deploy blocker, not a follow-up task.** Set *before* the first `railway up` that goes live, not after. This is the one item in this whole phase where "do it after and fix issues if they arise" is the wrong order — a spend cap costs nothing to set and is the actual backstop if Decision 3's gate has a gap.

**5. Railway is configured for exactly one instance, no autoscaling, and CORS is locked to the real Firebase origin instead of `"*"`.** `backend/app.py`'s `CORSMiddleware` currently wildcards origins because the client is a `file://` page locally (`docs/DECISIONS.md`) — once the client has a real deployed origin, `allow_origins` becomes `[settings.public_client_origin]` (a new config field), tightened rather than left wildcarded for a publicly reachable deployment. Local dev keeps `"*"` (or the local file origin) via an environment-conditional default, so nothing about local setup changes.

**6. `wss://`, not `ws://`, for the deployed `/bridge` connection — a mixed-content requirement, not a preference.** Firebase Hosting always serves over HTTPS; a browser will silently block a plain `ws://` connection from an HTTPS page. `client/config.js`'s `JUPUS_BRIDGE_WS_URL` for the deployed environment must be `wss://<railway-domain>/bridge`. Railway terminates TLS on its generated domain automatically, so this is a config value, not new backend work — flagged as its own decision anyway because it's exactly the kind of thing that "works fine locally, silently breaks in prod" and deserves being caught in design rather than live debugging.

**7. This phase does not attempt Phase 10's Twilio webhook hosting requirements — it's a shared prerequisite, tracked once, not duplicated.** If Phase 10 is built, its own webhook routes (`/telephony/incoming`, `/openai/sip-incoming`, etc.) ride on the same Railway deployment this phase stands up; Phase 10's doc references this one for the base hosting setup rather than re-specifying it.

---

## Config additions (`backend/config.py`)

```python
class Settings(BaseSettings):
    ...
    # Phase 9 (hosted deployment) — both Optional, both None for local dev
    # (where the gate/CORS-tightening below simply don't apply).
    jupus_access_token: Optional[str] = None
    public_client_origin: Optional[str] = None   # e.g. "https://<project>.web.app"
```

`.env.example` gains:
```
# Optional — only needed for a public hosted deployment (Phase 9)
# JUPUS_ACCESS_TOKEN=
# PUBLIC_CLIENT_ORIGIN=
```

---

## Changes to existing files

### `backend/app.py`
```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.public_client_origin] if settings.public_client_origin else ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

def verify_access_token(access_token: Optional[str] = None) -> None:
    # FastAPI dependency. No-op (always passes) when settings.jupus_access_token
    # is unset — local dev is unaffected. When set, requires an exact match,
    # raises HTTPException(401) otherwise.
    if settings.jupus_access_token and access_token != settings.jupus_access_token:
        raise HTTPException(status_code=401, detail="invalid or missing access token")

@app.post("/session", dependencies=[Depends(verify_access_token)])
async def create_session(...): ...   # unchanged body

@app.websocket("/bridge")
async def bridge(websocket: WebSocket, call_id: str, access_token: Optional[str] = None, ...):
    if settings.jupus_access_token and access_token != settings.jupus_access_token:
        await websocket.close(code=4401)
        return
    ...   # unchanged body past this check
```
`/admin` and `/admin/annotate` get the same `access_token` query-param check (a small dependency on the `StaticFiles` mount isn't directly possible — simplest is a tiny middleware scoped to the `/admin` path prefix, checked once rather than per-route, given `StaticFiles` serves many files under that mount).

### `client/config.example.js` (new, committed) / `client/config.js` (new, git-ignored)
```javascript
window.JUPUS_BACKEND_URL = "http://localhost:8000";
window.JUPUS_BRIDGE_WS_URL = "ws://localhost:8000/bridge";
window.JUPUS_ACCESS_TOKEN = "";   // only needed against a gated deployment
```
`client/index.html` adds `<script src="config.js"></script>` before `app.js`'s own `<script>` tag.

### `client/app.js`
```javascript
const BACKEND_URL = window.JUPUS_BACKEND_URL;
const BRIDGE_WS_URL = window.JUPUS_BRIDGE_WS_URL;
const ACCESS_TOKEN = window.JUPUS_ACCESS_TOKEN || "";
// ...
const sessionResp = await fetch(`${BACKEND_URL}/session${ACCESS_TOKEN ? `?access_token=${ACCESS_TOKEN}` : ""}`, ...);
// ...
ws = new WebSocket(`${BRIDGE_WS_URL}?call_id=${callId}${ACCESS_TOKEN ? `&access_token=${ACCESS_TOKEN}` : ""}`);
```

### `.gitignore`
Add `client/config.js` (real deployed values, including the access token, must never be committed — `client/config.example.js` is the committed placeholder).

---

## Deployment steps (manual, per Decision "not building CI/CD")

### Railway (backend)
1. `railway init` / link the repo, set the start command to the existing `uvicorn backend.app:app --host 0.0.0.0 --port $PORT` (Railway injects `$PORT`; `backend/config.py`'s `port` field already reads `JUPUS_PORT`, so confirm which one actually wins at runtime — Railway's injected `$PORT` should be treated as authoritative, since that's what its own routing expects the process to bind to).
2. Set environment variables: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `JUPUS_ACCESS_TOKEN`, `PUBLIC_CLIENT_ORIGIN` (the Firebase URL, known ahead of time since Firebase project names are chosen up front), `JUPUS_DB_PATH` pointed at the Volume mount path.
3. Attach a Railway Volume, mounted at the same path `JUPUS_DB_PATH` points to (Decision 1). Run `python backend/db/seed_slots.py` once against the deployed instance (Railway's shell/one-off command feature) to seed the calendar on the fresh Volume.
4. Confirm the instance count is set to 1, no autoscaling (Decision 5).
5. Set OpenAI/Anthropic billing spend caps and alerts (Decision 4) — before step 6.
6. Deploy (`railway up` or the connected-repo auto-deploy). Confirm `https://<project>.up.railway.app/session` responds (401 without the token, 200 with it) and `wss://<project>.up.railway.app/bridge` accepts a connection.

### Firebase Hosting (client)
1. `firebase init hosting`, public directory `client/`.
2. Create the real `client/config.js` (git-ignored) pointing `JUPUS_BACKEND_URL`/`JUPUS_BRIDGE_WS_URL` at the Railway domain (`wss://`, per Decision 6) and `JUPUS_ACCESS_TOKEN` at the same value set in Railway's env vars.
3. `firebase deploy --only hosting`.
4. Confirm the deployed Firebase URL matches exactly what was set in Railway's `PUBLIC_CLIENT_ORIGIN` (CORS will silently reject the browser's requests otherwise — the single most likely first-deploy failure mode for this setup, worth calling out plainly rather than discovering it live).

---

## Tests

Nothing here needs new automated tests beyond what already exists — this phase is deploy configuration and two small, already-tested-shape code changes (a FastAPI dependency, a WebSocket query-param check). What it does need is targeted unit coverage for the new gate logic itself:

### `backend/tests/test_access_gate.py` (new file)
1. `test_session_allowed_without_token_when_unset` — `settings.jupus_access_token is None`; `/session` succeeds with no `access_token` param (local dev is unaffected).
2. `test_session_rejects_missing_token_when_configured` — `jupus_access_token` set; a request with no `access_token` param gets 401.
3. `test_session_rejects_wrong_token_when_configured`.
4. `test_session_accepts_correct_token_when_configured`.
5. `test_bridge_ws_closes_on_missing_or_wrong_token_when_configured` — matching coverage for the WebSocket path (close code, not an HTTP status).
6. `test_bridge_ws_accepts_correct_token_when_configured`.
7. `test_admin_routes_gated_the_same_way` — the `/admin` path-prefix middleware, same missing/wrong/correct-token coverage.

---

## Definition of Done

- [x] `pytest backend/tests/test_access_gate.py` and full suite pass, zero regressions (local dev's unset-token behavior is the thing most likely to accidentally regress — verify explicitly).
- [x] Live: deployed Firebase client successfully completes a full real call against the deployed Railway backend — booking, escalation, and low-confidence-capture all re-verified against the *hosted* deployment, not assumed to work just because they work locally (network/CORS/mixed-content issues are exactly the class of bug that only shows up once actually deployed).
- [x] Live: hitting the Railway `/session` URL directly with no `access_token` returns 401; with the wrong token, 401; confirms the gate is actually live, not just present in code.
- [x] Live: restart/redeploy the Railway service; confirm previously-booked slots and call history in `/admin` survive the redeploy (proves the Volume is actually mounted and used, not just configured).
- [x] OpenAI and Anthropic spend caps/alerts confirmed set, screenshotted or otherwise recorded for your own records (not something to just remember doing).
- [x] `docs/DECISIONS.md` updated: the SQLite-on-a-Volume-not-Postgres call (Decision 1, this is where the earlier conversation's reasoning gets formally recorded), and the access-gate threat model (Decision 3 — explicitly "deters casual discovery/abuse of a time-boxed demo URL," not "production auth").
- [x] README gets a short "Try it live" section with the Firebase URL and a plain note that the access token is required and where to get it (i.e. ask the project owner) — the hosted deployment is an addition to the existing local-setup instructions, which stay unchanged and remain the primary path per this doc's Goal.
- [x] `client/config.example.js` committed, `client/config.js` confirmed git-ignored (a real access token accidentally committed is the one genuinely bad outcome to guard against here).

---

## Note on relationship to Phase 10 (telephony)

If Phase 10 is built after this phase, its `public_base_url` config value is this same Railway domain, and its webhook routes are added to the same already-gated, already-CORS-configured backend — this phase is the one-time hosting setup; Phase 10 only adds routes on top of it. If Phase 10 is built *before* this phase (unlikely given Phase 10's own DoD already assumes hosting exists), this phase's Railway setup steps would need to happen first regardless, just reordered.
