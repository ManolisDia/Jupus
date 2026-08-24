#!/usr/bin/env python3
"""Blocks a commit if the staged diff contains something shaped like a
real API key. .env is gitignored, but that doesn't protect against a key
accidentally pasted into a test fixture, a debug print, or a doc example.
Not a substitute for care — a determined mistake can still slip past a
regex — but it catches the common accident cheaply.
"""
import re
import subprocess
import sys

KEY_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9\-_]{20,}"),  # Anthropic
    re.compile(r"sk-[A-Za-z0-9]{20,}"),          # OpenAI
]


def main() -> int:
    diff = subprocess.run(
        ["git", "diff", "--cached"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    ).stdout

    hits = [p.pattern for p in KEY_PATTERNS if p.search(diff)]
    if hits:
        print("Staged diff contains something shaped like a real API key — aborting commit.")
        print("If this is a false positive (e.g. an obviously fake example key), rename it")
        print("to something that doesn't match a real key prefix, or use --no-verify deliberately.")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
