# The Benevolent Dictator

## The idea

Eval taxonomies drift when multiple people shape "what a good conversation sounds like" in different, sometimes conflicting directions. The fix: one designated domain expert is the single source of truth. They annotate real conversations directly, their judgment is what the LLM judge is calibrated against, and they hold sole authority to approve changes to the error taxonomy. Not a committee, not a vote — one person's calibrated judgment, which is far more coherent over time than several people's uncoordinated opinions.

For Jupus, the Benevolent Dictator ("BD") doesn't need deep legal expertise — the thing being judged isn't legal correctness, it's *conversation quality*: did the agent route sensibly, capture details accurately, confirm before acting, escalate appropriately. That's a judgment call about the interaction, which is exactly the taxonomy in `docs/error_taxonomy.md`. In this project, that's you.

`backend/config.py` gets one new setting: `annotator_name: str = "benevolent_dictator"` (a fixed identity string written onto every annotation — not a real auth system, just a label, since this is a single-user local tool).

## What the BD actually does

1. **Annotates calls, whenever, not just after an eval run.** The admin panel's annotation page (`/admin/annotate`, see Phase 6c) shows a queue of calls that haven't been reviewed yet — pulled from real usage or from a batch you've just run — independent of whether `run_eval.py`'s LLM judge has looked at them yet or not. Annotation doesn't wait on the judge, and the judge doesn't wait on annotation; they're two independent passes over the same calls.

2. **For each call, records**: which of the current `error_classes.py` classes actually apply (a simple checklist, can be zero), and — critically — a free-text note for **any issue that doesn't fit an existing class at all**. That second case is the single most valuable signal this whole system produces: it's a domain expert saying "something's wrong here and our taxonomy doesn't have a name for it yet," which is exactly the raw material `propose_taxonomy_updates` needs.

3. **Marks selected calls as gold examples.** A gold call is one the BD is confident enough about to use as ground truth — either "this is clean" (reviewed, zero flags) or "this specific set of flags is correct." Gold calls are what `eval/calibrate_judge.py` measures the LLM judge against.

4. **Is the sole approver of taxonomy changes.** `taxonomy_suggestions` (LLM-generated, see Phase 6c) get a `status` of `pending` until the BD approves or rejects them in the admin panel. Only an approved suggestion should ever result in a hand-edit to `eval/error_classes.py` — the LLM proposes, the BD decides, never the reverse.

## How BD input feeds the taxonomy-critique pass

`propose_taxonomy_updates` (Phase 6c) takes the batch's LLM classifications **and** any `human_annotations` for calls in that batch as joint input. Two signals matter most:
- **Human flagged `error_class_id = NULL` with a note** (an issue with no fitting class) → strong evidence for a `new_class` suggestion, using the BD's note as the rationale.
- **Human and LLM disagree on a call** (BD flagged a class the judge missed, or didn't flag one the judge did) → evidence for either a `misclassification` suggestion (isolated case) or a `refine_existing` suggestion (if the same disagreement pattern recurs across multiple calls in the batch — a sign the class's description is ambiguous, not that the judge made one mistake).

This means the taxonomy doesn't just evolve from the LLM second-guessing itself — its strongest signal is a domain expert's direct disagreement, which is what actually prevents the taxonomy (and the judge) from drifting away from what "good" means in practice.

## Calibration, not blind trust

`eval/calibrate_judge.py` compares the LLM judge's `call_error_flags` against the BD's `human_annotations` for every call the BD has reviewed, and reports per-class agreement (true/false positive/negative against the human's labels). This number is a real health signal, same tier as the error-rate regression check from `eval/compare_runs.py`: if calibration drops after a taxonomy edit or a prompt change, that's worth investigating before trusting the judge's output on new calls. See Phase 6 for the exact mechanics.
