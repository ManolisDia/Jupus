# Workflow — Branching, Commits, Verification

How work actually gets done phase by phase: branch per phase, commit far more often than "once it's done," and an independent verification pass before anything merges into `master`. This governs *process*, not architecture — read alongside `CLAUDE.md`.

---

## Branching

- `master` is always the last **fully-verified** phase — every commit on it has passed that phase's complete Definition of Done (tests green + manual checks done). Never commit directly to `master` mid-phase.
- Each phase (and sub-phase) gets its own branch off `master`, named after its doc: `phase-1-raw-voice-loop`, `phase-2-supervisor-skeleton`, `phase-6a-observability`, `phase-6b-error-taxonomy`, `phase-6c-benevolent-dictator`, etc. — the branch name always tells you which spec in `docs/phases/` governs it.
- When a phase's full DoD is verified (see below), merge with `git merge --no-ff <branch>` — **not** a squash. Keeping the granular commit history visible is deliberate: a methodical, frequently-committed, test-first history is itself part of what you can point to in the submission, and squashing throws that away for no benefit on a solo project.
- Branches can be deleted after merge or left around — only `master`'s merge history matters going forward.

## Commit frequency and granularity

Once per phase is too coarse. Commit after any of these, not just at the end of a session:
- A test file's tests go from red (failing or nonexistent) to green.
- One function/class/file is implemented and at least manually sanity-checked.
- A DoD checklist item flips from unchecked to checked.
- Right before a risky or wide-reaching change (schema migration, a refactor touching several files) — so there's a clean revert point.
- A doc gets updated because implementation diverged from the original phase spec (should be rare, but real when it happens).

Rule of thumb: if the commit message needs "and" to describe it, it's probably two commits. Never bundle unrelated changes together.

## Commit messages

Conventional-commit style, scoped by phase:
- `feat(phase-3): implement classify_practice_area with confidence threshold`
- `test(phase-3): add capture node confirm-back tests`
- `fix(phase-2): correct transcript reducer to append not replace`
- `docs(phase-5): log filler-phrase verification result in known-issues`

No `Co-Authored-By` trailer on commits.

## Enforcement — three layers, not just documentation

`CLAUDE.md`'s standing rules and this doc's commit/branch conventions are instructions a Claude Code session is expected to follow — but instructions alone are soft enforcement; nothing stops a session from drifting except its own discipline. Real enforcement here is layered:

1. **Pre-commit hooks (`.pre-commit-config.yaml`) — automated, blocks the commit outright, for the mechanically checkable subset.** `pre-commit install` (one-time, see `CLAUDE.md`'s run commands) wires up three hooks that run on every `git commit`:
   - `pytest` (`backend/tests` + `eval/tests`) — a commit cannot land if the suite is red. This is also what makes the "commit at green, not at the end of a session" convention above actually true rather than aspirational.
   - `scripts/check_architecture.py` — greps the staged diff for the two doctrine violations a regex can reliably catch: raw `sqlite3` usage outside `backend/db/repositories/` (rule 9), and direct Anthropic SDK calls outside `llm_utils.py` (rule 7).
   - `scripts/check_no_secrets.py` — blocks a commit whose staged diff contains something shaped like a real API key, independent of `.env` being gitignored.

   **Be honest about what this doesn't catch.** A regex can't verify "every tool call goes through `traced_call`" (rule 8), "each node binds only its own scoped tools" (rule 5), or "Realtime sees exactly one tool" (rule 1) — those are about control flow and intent, not a string pattern. Don't add a false sense of security by pretending pre-commit covers the whole doctrine; it covers exactly two rules well, and that's the point of layer 2.

2. **Independent subagent DoD review — the semantic/structural rules, before a phase branch merges.** Covered above: a fresh subagent, given the phase doc and the diff, explicitly checks the rules pre-commit can't — tool scoping, `traced_call`/`call_claude_tool` usage patterns, single-tool-on-Realtime. This is what closes the gap pre-commit leaves open, and it happens once per phase (at merge time), not once per commit.

3. **`CLAUDE.md` itself — the baseline the first two layers are checking against.** Auto-loaded at the start of every Claude Code session in this repo, this is where the rules are actually defined; layers 1–2 exist because relying on a session simply remembering and following them turn after turn isn't enough on its own.

If you want an additional nudge at the Claude Code session level itself (e.g. a reminder if a session is about to end with uncommitted changes, or with pre-commit not yet installed) — that's a `.claude/settings.json` hook, and the right way to set that up correctly is the `update-config` skill from inside an actual Jupus session (`/update-config` or just asking for it), not a hand-authored hook config guessed at from outside that session's context.

## Verification — don't trust the builder's own "looks done"

The agent that just wrote the code is biased toward believing it satisfies the spec. Before merging a phase branch into `master`, run an independent check, in this order:

1. **Automated tests.** `pytest` for that phase's test files, fully green. Just run it — this doesn't need a subagent, it's one command.
2. **Independent DoD review — a fresh subagent, not the builder.** Spawn a new agent with no memory of having written the code. Give it exactly: the phase's doc (`docs/phases/phase-N-*.md`) and the diff/changed files since branching from `master`. Ask it to go through the DoD checklist item by item and report which are genuinely satisfied vs. claimed-but-unverified vs. missing — and separately check for architecture-doctrine violations (`CLAUDE.md`'s numbered rules: raw SQL outside `backend/db/repositories/`, a tool call bypassing `traced_call`, a Claude call bypassing `call_claude_tool`, an extra tool exposed to Realtime, etc.). A fresh, adversarial read catches "I'm pretty sure this works" gaps the original context is blind to.
3. **Manual/live checks.** Anything in the DoD needing an actual live call, a human ear, or eyeballing the admin panel is yours to do — this can't be delegated to a subagent.
4. Only once 1–3 all pass: merge.

## When (not) to fan out multiple building agents

Most of this build is inherently sequential — each phase depends on the previous phase's *verified* state, and within a phase, most files depend on each other (schema before repository before dispatcher before tests). Fanning out parallel building agents within a phase mostly just risks conflicting edits to shared files (`state.py`, `schema.sql`) for no real speed benefit. Don't do it as a default.

Where fanned-out agents genuinely help is **independent verification and narrow research, not construction**:
- The DoD-review subagent above.
- A quick pre-commit scan of `git diff --cached` for anything that looks like a leaked key/token — cheap, worth doing as a habit, especially since `.env` being gitignored doesn't protect against a stray hardcoded key elsewhere.
- Narrow, genuinely independent research the phase docs already flag as "confirm at implementation time" (exact OpenAI Realtime event names, `semantic_vad` field names) — fine to delegate to a subagent while the main agent keeps building something unrelated.

## Worktrees

Not needed for the main phase-by-phase build — the dependency chain is strictly linear (`docs/PLAN.md`'s phase index), so there's no genuine parallel-phase work to isolate; a second worktree building "ahead" of the current phase would just be building against an unverified foundation. Reach for one only for a bounded side experiment you don't want touching the current branch — e.g. trying Phase 5's stretch (dynamic turn-detection eagerness) in isolation, or testing a risky prompt rewrite before deciding whether to bring it into the phase branch properly.
