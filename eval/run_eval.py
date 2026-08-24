"""python eval/run_eval.py --label <name> [--calls all|new]

Runs the deterministic + classification + taxonomy-critique passes over a
batch of calls and tags them with an eval_run_label for later comparison
(eval/compare_runs.py, Phase 6c). See docs/phases/phase-6b-error-taxonomy.md
(steps 1-4) and docs/phases/phase-6c-benevolent-dictator.md (step 5).

  --calls new (default): calls with a real outcome not yet present in
      eval_runs for ANY label.
  --calls all: re-judge every call with outcome IS NOT NULL, regardless of
      prior runs — useful when the taxonomy itself changed.
"""

import argparse
import json

from backend.config import settings
from backend.db.repositories import Repositories, get_repositories
from eval.insights_agent import (
    compute_error_rates,
    run_classification_pass,
    run_deterministic_pass,
    run_taxonomy_critique,
)


def select_calls(repos: Repositories, mode: str) -> list[dict]:
    all_calls = repos.calls.list(with_outcome_only=True)
    if mode == "all":
        return all_calls
    already_evaluated = repos.evals.call_ids_already_evaluated()
    return [c for c in all_calls if c["call_id"] not in already_evaluated]


def run(repos: Repositories, label: str, calls_mode: str = "new") -> dict:
    """The actual eval-run logic, factored out of main() so tests can drive
    it directly against an injected Repositories without going through argv
    or a real get_repositories(settings) call."""
    calls = select_calls(repos, calls_mode)

    for call in calls:
        repos.evals.tag_eval_run(call["call_id"], label)
    print(f"Tagged {len(calls)} call(s) with eval_run_label={label!r} (--calls {calls_mode})")

    deterministic = run_deterministic_pass(repos, calls)
    print("\nDeterministic stats:")
    print(json.dumps(deterministic, indent=2))

    batch_results = run_classification_pass(repos, calls, label)
    failed = [r["call_id"] for r in batch_results if r.get("classification_failed")]
    if failed:
        print(f"\n{len(failed)} call(s) FAILED classification and were skipped (see logs): {failed}")

    error_rates = compute_error_rates(repos, label)
    print("\nError rates:")
    for class_id, rate in sorted(error_rates.items()):
        print(f"  {class_id}: {rate:.1%}")

    suggestions = run_taxonomy_critique(repos, batch_results, label)
    print(
        f"\n{len(suggestions)} new taxonomy_suggestions row(s) for label={label!r} — "
        "review them in the admin panel (they are 'pending' until approved/rejected)."
    )

    return {
        "calls": calls,
        "deterministic": deterministic,
        "batch_results": batch_results,
        "error_rates": error_rates,
        "suggestions": suggestions,
    }


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", required=True, help="eval_run_label to tag this batch with")
    parser.add_argument("--calls", choices=["all", "new"], default="new")
    args = parser.parse_args(argv)

    repos = get_repositories(settings)
    run(repos, args.label, args.calls)


if __name__ == "__main__":
    main()
