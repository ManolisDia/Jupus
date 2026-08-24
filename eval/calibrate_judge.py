"""python eval/calibrate_judge.py [--label <eval_run_label>]

Compares the LLM judge's call_error_flags against the Benevolent Dictator's
human_annotations for every call the BD has reviewed — docs/benevolent_dictator.md,
docs/phases/phase-6c-benevolent-dictator.md. This is a real health signal: if
calibration drops after a taxonomy edit or a prompt change, that's worth
investigating before trusting the judge's output on new calls.

Pools raw true/false positive/negative counts per error class across every
reviewed call (optionally scoped to one eval_run_label), and reports raw
counts alongside precision/recall — a tiny annotated sample shouldn't be
dressed up as a precise percentage.
"""

import argparse

from backend.config import settings
from backend.db.repositories import Repositories, get_repositories
from eval.error_classes import get_active_error_classes


def _human_flagged_classes(review: dict) -> set[str]:
    return {a["error_class_id"] for a in review.get("annotations", []) if a.get("error_class_id")}


def _human_uncategorized_count(review: dict) -> int:
    return sum(1 for a in review.get("annotations", []) if a.get("error_class_id") is None)


def build_calibration(repos: Repositories, label: str | None = None) -> dict:
    reviewed_calls = []
    for call in repos.calls.list():
        review = repos.annotations.get_review(call["call_id"])
        if review is not None:
            reviewed_calls.append((call["call_id"], review))

    counts = {c["id"]: {"tp": 0, "fp": 0, "fn": 0} for c in get_active_error_classes()}
    uncategorized_count = 0

    for call_id, review in reviewed_calls:
        human_set = _human_flagged_classes(review)
        uncategorized_count += _human_uncategorized_count(review)

        all_flags = repos.evals.get_error_flags(call_id)
        if label is not None:
            all_flags = [f for f in all_flags if f["eval_run_label"] == label]
        llm_set = {f["error_class_id"] for f in all_flags}

        for class_id in counts:
            in_human = class_id in human_set
            in_llm = class_id in llm_set
            if in_human and in_llm:
                counts[class_id]["tp"] += 1
            elif in_llm and not in_human:
                counts[class_id]["fp"] += 1
            elif in_human and not in_llm:
                counts[class_id]["fn"] += 1

    report = {}
    for class_id, c in counts.items():
        tp, fp, fn = c["tp"], c["fp"], c["fn"]
        precision = tp / (tp + fp) if (tp + fp) > 0 else None
        recall = tp / (tp + fn) if (tp + fn) > 0 else None
        report[class_id] = {
            "true_positive": tp, "false_positive": fp, "false_negative": fn,
            "precision": precision, "recall": recall,
        }

    return {
        "reviewed_call_count": len(reviewed_calls),
        "uncategorized_note_count": uncategorized_count,
        "per_class": report,
    }


def print_calibration(calibration: dict) -> None:
    print(f"Reviewed calls: {calibration['reviewed_call_count']}")
    print(f"BD 'doesn't fit any class' notes: {calibration['uncategorized_note_count']}")
    print()
    print(f"{'class':<35}{'TP':>5}{'FP':>5}{'FN':>5}{'precision':>12}{'recall':>10}")
    for class_id, row in calibration["per_class"].items():
        precision_str = f"{row['precision']:.0%}" if row["precision"] is not None else "n/a"
        recall_str = f"{row['recall']:.0%}" if row["recall"] is not None else "n/a"
        print(
            f"{class_id:<35}{row['true_positive']:>5}{row['false_positive']:>5}{row['false_negative']:>5}"
            f"{precision_str:>12}{recall_str:>10}"
        )


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", default=None, help="scope llm flags to one eval_run_label (default: any label)")
    args = parser.parse_args(argv)

    repos = get_repositories(settings)
    calibration = build_calibration(repos, args.label)
    print_calibration(calibration)


if __name__ == "__main__":
    main()
