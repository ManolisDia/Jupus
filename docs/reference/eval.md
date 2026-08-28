# The Eval Pipeline

Transcripts do not tell you whether a voice agent is good. This is the machinery that does.

Five ideas hold it together:

1. **A small, editable error taxonomy** — four classes, in code, versioned.
2. **An LLM judge** that classifies each completed call against it, reading the *trace* rather than the transcript.
3. **A labelled-batch model**, so two runs can be compared and a prompt change can be validated instead of guessed at.
4. **A human annotator** — the Benevolent Dictator — whose labels calibrate the judge and who alone approves taxonomy changes.
5. **A hard rule: the system never auto-mutates its own taxonomy.**

---

## The taxonomy

`eval/error_classes.py`. Four classes, each with a stable `id`, a `name`, and a long `description` that goes verbatim into the judge prompt.

| id | Catches |
|---|---|
| `repetition` | Re-asks for an already-confirmed field, or repeats substantially the same question without new ambiguity from the caller |
| `tool_or_system_failure_surfaced` | A generic fallback reply fired, an escalation with `reason="system_error"`, or a reply that does not logically follow |
| `premature_escalation` | Escalated to a human when the transcript suggests the need was answerable |
| `unconfirmed_action` | A booking made, or a field treated as confirmed, with no read-back-and-assent turn |

`unconfirmed_action`'s description carries an important carve-out: `name` auto-confirming at ≥0.75 with no read-back is the **documented, deliberate design**, not a violation. Only email/phone confirmed without a read-back, or a sub-threshold auto-confirm, count.

### Rules for editing it

- **`id` is permanent once used.** Historical `call_error_flags` rows reference it, so renaming an id breaks the ability to compare old and new runs. Edit `name` and `description` freely; rename an `id` never.
- **Retire, do not delete.** Add `"active": False` and let `get_active_error_classes()` filter it, so old runs stay interpretable. Nothing is inactive yet; the hook exists for when something is.
- **This file is the single source of truth** for both the judge prompt and the admin UI. There is no second copy of the descriptions to keep in sync.

---

## `insights_agent.py` — the three passes

`eval/insights_agent.py` never imports `sqlite3`; everything goes through an injected `Repositories`.

### 1. Deterministic — `run_deterministic_pass(repos, calls)`

No LLM. Also serves `GET /api/eval/summary`.

```python
{
  "booking_success_rate": ...,        # booked / (booked + escalated)
  "escalation_reason_histogram": ..., # {reason: count}
  "average_turns_per_call": ...,
  "latency": {...},                   # p50/p95/avg per stage
  "cost": {...}                       # average/p50/p95 USD
}
```

Every one of these never raises on empty or malformed input — a bad `transcript_json` counts as 0 turns for that call rather than killing the pass.

### 2. Classification — `run_classification_pass(repos, calls, label)`

For each call: `classify_call_errors(call_row, trace, error_classes)` → persist any flags with the run label. An empty flag list is valid and expected.

Two details that matter:

- **The judge reads the trace, not the transcript.** Two `tool_call_start` events for the same already-confirmed field is a far stronger repetition signal than surface text.
- **`max_tokens=4096`, not the 512 default.** Reasoning over a full trace reliably burns well past 512 on extended thinking alone before emitting anything. Confirmed live: a real trace consumed exactly 512 thinking tokens and returned `stop_reason="max_tokens"` with no text content block at all.

One call failing does not abort the batch — it is logged, recorded as `classification_failed`, and the run continues.

### 3. Taxonomy critique — `run_taxonomy_critique(repos, batch_results, label)`

Takes the batch's own classifications **plus** any BD annotations for those calls, and asks `propose_taxonomy_updates` for suggestions. Every suggestion is persisted with `status="pending"`.

The prompt weights human judgment above the model's self-critique:

- A human flag of `error_class_id = NULL` with a note → strong evidence for a **`new_class`**, using their note as the rationale.
- One judge/human disagreement on one call → a **`misclassification`**.
- The *same* disagreement pattern recurring across calls → a **`refine_existing`**, because the class description is probably ambiguous rather than the judge being wrong once.

A failure here returns `[]` rather than raising. The critique is a nice-to-have on top of a classification pass that already ran and already persisted real work; it must not throw that away.

> Note this pass records its trace events under a synthetic call id, `"eval_run:" + label` — it is one call over the whole batch, not per-call.

---

## The CLIs

### `run_eval.py` — judge a batch

```bash
python eval/run_eval.py --label demo
python eval/run_eval.py --label demo --calls all
```

Tags the selected calls with `label`, then runs all three passes and prints the results.

- `--calls new` (default): calls with a real outcome not yet present in `eval_runs` under **any** label.
- `--calls all`: re-judge every call with a non-null outcome. Use this after changing the taxonomy.

### `replay_scenarios.py` — build a comparable batch

```bash
python eval/replay_scenarios.py --label baseline
```

Drives the seven canonical scenarios through the **real, unmocked** pipeline: live Claude calls, real money. That is S1–S6 plus S7, whose two variants S7a (citation found) and S7b (none found) run separately — eight runs in total. Each resulting call is tagged with `(label, scenario_id)`.

It awaits `dispatcher.run_supervisor_turn` directly, turn by turn, rather than going through the transport: there is no caller audio to keep servicing, and awaiting each turn keeps the utterances strictly ordered. It also explicitly awaits any outstanding `STATUTE_SEARCHES` task before finishing, because otherwise the script races its own exit and the grounding call's trace events are captured only by luck.

> **The scripts encode the exact turn structure the current flow produces.** They were rewritten for Phase 7's fast-pass shape (name, email, phone back to back, then a batched drain) and again for Phase 8's research stage. If you change the number of turns a stage takes, these scripts desynchronise and every later utterance answers the wrong question. `backend/tests/test_scenarios.py` mirrors them and will usually catch it first.

### `compare_runs.py` — validate a change

```bash
python eval/replay_scenarios.py --label baseline
# ... make the prompt change ...
python eval/replay_scenarios.py --label after-tweak
python eval/compare_runs.py --baseline baseline --candidate after-tweak
```

Prints per-class baseline rate, candidate rate, delta, and a regression flag. **Exits 1 if any class regressed by more than `--threshold` (default 0.1)**, so it works as a CI-style gate.

This is the procedure for shipping a prompt change or a per-tool model swap. Both Haiku swaps in the codebase went through it.

### `calibrate_judge.py` — is the judge any good?

```bash
python eval/calibrate_judge.py [--label <run>]
```

For every call the BD has reviewed, compares the judge's flags against the human's and pools TP / FP / FN per class, reporting **raw counts alongside** precision and recall — a tiny annotated sample should not be dressed up as a precise percentage. Precision and recall are `None` (printed `n/a`) on a zero denominator.

Also reports `uncategorized_note_count` — how many times the BD said "something is wrong and we have no class for it". That number going up is the clearest signal the taxonomy needs work.

### The other three

| Script | Does |
|---|---|
| `concurrency_stress_test.py` | N concurrent independent calls through `run_supervisor_turn`. Proves no cross-call state leakage. Uses its own DB file, never `calendar.db`. `--mode live` is capped at N=10. |
| `livekit_live_call.py` | Real LiveKit calls with **synthesized caller speech**, no human at a mic. The only test that crosses the transport boundary. |
| `filler_latency_report.py` | Perceived wait vs. actual round trip, from real traces. |

---

## The human in the loop

Full rationale in [`../benevolent_dictator.md`](../benevolent_dictator.md). The mechanics:

1. **Annotate** at `/admin/annotate`, any time — independent of whether the judge has looked at the call. The queue is `GET /api/calls/unreviewed`.
2. **Record** which existing classes apply (possibly zero), and — critically — a free-text note for anything that fits *no* existing class.
3. **Mark gold** calls the BD is confident enough about to use as ground truth. Those are what `calibrate_judge.py` measures against.
4. **Approve or reject** taxonomy suggestions in the admin panel.

**Approval is not application.** Flipping a suggestion to `approved` changes a row's status and nothing else. Applying it means a human hand-editing `eval/error_classes.py`. The LLM proposes; the BD decides; nothing auto-applies. An LLM judge grading its own system and rewriting its own rubric is a closed loop, and this is the thing that opens it.

The identity is a fixed label from `settings.annotator_name` (`"benevolent_dictator"`), not an auth system. Single-user local tool.

---

## The full loop

```
   live calls ──┐
                ├──► calls + trace_events ──► run_eval.py ──► call_error_flags
replay_scenarios┘            │                                      │
                             │                                      ├──► compute_error_rates
                             ▼                                      │         │
                      /admin/annotate                               │         ▼
                             │                                      │   compare_runs.py
                             ▼                                      │   (regression gate)
                  call_reviews + human_annotations ─────┬───────────┤
                                                        │           │
                                                        ▼           ▼
                                          propose_taxonomy_updates  calibrate_judge.py
                                                        │           (precision/recall)
                                                        ▼
                                             taxonomy_suggestions (pending)
                                                        │
                                                   BD approves
                                                        │
                                                        ▼
                                    HUMAN hand-edits eval/error_classes.py
                                                        │
                                                 (re-run the judge)
```

---

## Adding an error class

1. Append to `ERROR_CLASSES` in `eval/error_classes.py` with a new stable `id` and a description precise enough that two people would apply it the same way. It goes verbatim into the judge prompt.
2. Re-run the judge over historical calls: `python eval/run_eval.py --label <new> --calls all`.
3. Check calibration: `python eval/calibrate_judge.py`.
4. Update the table in [`../error_taxonomy.md`](../error_taxonomy.md).
5. Nothing else. The admin UI reads the registry through `GET /api/eval/error-classes`, and `compute_error_rates` iterates `get_active_error_classes()`, so both pick it up automatically.

---

## Known limits

- **Hand-seeded demo calls have no `trace_events`**, and the judge reads only the trace — so it will never flag them, no matter how obviously broken their transcripts are (`docs/known-issues/2026-08-24-001.md`). Use `replay_scenarios.py` for anything the judge is meant to actually score.
- **A few "fully mocked" scenario tests still hit the real Anthropic API on turn 1**, via the greeting→routing dispatch chaining (`docs/known-issues/2026-08-25-002.md`). Pre-existing and open.
- **`replay_scenarios.py` intermittently loses the background statute-search task** entirely — no error, no trace, no result — and it has not been reproduced when the same utterances are driven directly (`docs/known-issues/2026-08-25-003.md`).
- **Prices in `eval/pricing.py` are hardcoded** and not kept in sync automatically. Verify before quoting a dollar figure.
