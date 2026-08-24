"""python eval/compare_runs.py --baseline <label> --candidate <label> [--threshold 0.1]

Diffs per-error-class rates between two labeled eval runs to catch
regressions after a prompt-engineering change — docs/phases/
phase-6c-benevolent-dictator.md. Typical workflow:
    python eval/replay_scenarios.py --label baseline
    ... make a prompt change ...
    python eval/replay_scenarios.py --label after-tweak
    python eval/compare_runs.py --baseline baseline --candidate after-tweak

Exit code 1 if any class's rate increased by more than --threshold, 0
otherwise — usable as a CI-style gate.
"""

import argparse

from backend.config import settings
from backend.db.repositories import Repositories, get_repositories

DEFAULT_THRESHOLD = 0.1


def build_comparison(repos: Repositories, baseline: str, candidate: str, threshold: float = DEFAULT_THRESHOLD) -> dict:
    baseline_rates = repos.evals.compute_error_rates(baseline)
    candidate_rates = repos.evals.compute_error_rates(candidate)

    rows = []
    any_regression = False
    for class_id in sorted(set(baseline_rates) | set(candidate_rates)):
        baseline_rate = baseline_rates.get(class_id, 0.0)
        candidate_rate = candidate_rates.get(class_id, 0.0)
        delta = candidate_rate - baseline_rate
        regressed = delta > threshold
        any_regression = any_regression or regressed
        rows.append(
            {
                "error_class_id": class_id,
                "baseline_rate": baseline_rate,
                "candidate_rate": candidate_rate,
                "delta": delta,
                "regressed": regressed,
            }
        )

    return {
        "baseline": baseline,
        "candidate": candidate,
        "threshold": threshold,
        "rows": rows,
        "any_regression": any_regression,
    }


def print_comparison(comparison: dict) -> None:
    print(f"{'class':<35}{'baseline':>10}{'candidate':>10}{'delta':>10}  regressed?")
    for row in comparison["rows"]:
        print(
            f"{row['error_class_id']:<35}{row['baseline_rate']:>10.1%}{row['candidate_rate']:>10.1%}"
            f"{row['delta']:>+10.1%}  {'YES' if row['regressed'] else 'no'}"
        )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    args = parser.parse_args(argv)

    repos = get_repositories(settings)
    comparison = build_comparison(repos, args.baseline, args.candidate, args.threshold)
    print_comparison(comparison)

    if comparison["any_regression"]:
        print(f"\nPossible regression(s) detected (delta > {args.threshold:.0%}).")
        return 1
    print("\nNo regressions detected.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
