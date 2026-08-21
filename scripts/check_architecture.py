#!/usr/bin/env python3
"""Static enforcement of the mechanically-checkable subset of CLAUDE.md's
architecture doctrine. Run via pre-commit on every commit touching .py files.

This deliberately does NOT try to check everything in CLAUDE.md — rules
that require understanding control flow or intent (e.g. "every tool call
goes through traced_call", "each node binds only its own scoped tools")
can't be reliably grep-checked and will pass this script even if violated.
Those are caught by the independent DoD-review subagent before a phase
branch merges into master (see docs/workflow.md), not here. This script
only catches the subset where a false negative would be embarrassing and
a regex is actually reliable: raw sqlite3 usage leaking out of the
repository layer, and the Anthropic SDK being called directly instead of
through the retry/trace wrapper.
"""
import re
import subprocess
import sys

REPOSITORIES_DIR = "backend/db/repositories/"
LLM_UTILS_FILE = "backend/supervisor/llm_utils.py"

SQLITE_PATTERN = re.compile(r"\bimport sqlite3\b|\bsqlite3\.connect\s*\(")
ANTHROPIC_DIRECT_PATTERN = re.compile(r"\banthropic\.Anthropic\s*\(|\.messages\.create\s*\(")


def staged_python_files() -> list[str]:
    out = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
        capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    return [f for f in out if f.endswith(".py")]


def read_staged_content(path: str) -> str:
    # Read the STAGED version, not the working-tree version — what's about
    # to be committed, not whatever's currently on disk.
    result = subprocess.run(
        ["git", "show", f":{path}"], capture_output=True, text=True,
    )
    return result.stdout if result.returncode == 0 else ""


def main() -> int:
    violations: list[str] = []

    for path in staged_python_files():
        content = read_staged_content(path)

        if not path.startswith(REPOSITORIES_DIR) and SQLITE_PATTERN.search(content):
            violations.append(
                f"{path}: imports/uses sqlite3 outside {REPOSITORIES_DIR} "
                f"(CLAUDE.md rule 9 — all SQL must live in a repository class)"
            )

        if path != LLM_UTILS_FILE and ANTHROPIC_DIRECT_PATTERN.search(content):
            violations.append(
                f"{path}: calls the Anthropic SDK directly, bypassing call_claude_tool "
                f"(CLAUDE.md rule 7 — every Claude call must go through llm_utils.py)"
            )

    if violations:
        print("Architecture doctrine violations found (see CLAUDE.md):\n")
        for v in violations:
            print(f"  - {v}")
        print(
            "\nThis check is a fast, incomplete net — it only catches raw "
            "sqlite3/Anthropic SDK leaks. It does not verify traced_call "
            "usage, tool scoping, or single-tool-on-Realtime — those are "
            "verified by an independent subagent review before a phase "
            "branch merges into master (docs/workflow.md)."
        )
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
