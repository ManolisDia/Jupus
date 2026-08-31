"""Suite-wide guards.

`tools.write_handoff_note` writes into `docs/handoffs/`, which is a COMMITTED
directory (see `docs/reference/operations.md`'s runtime-paths table) — unlike
the database and the logs, those notes are part of the repo. So any test that
reaches `node_escalation`, or the dispatcher's unhandled-exception path,
rewrites a *tracked* file with a live timestamp. The working tree is then
dirty after every `pytest` run, and pre-commit's stash/restore fights the
pytest hook that just caused it: the hook modifies the same file it stashed,
and the restore conflicts.

Most escalation tests already patch `HANDOFFS_DIR` at a temp directory
one-by-one (`test_escalation_node.py`, `test_dispatcher_async.py`). This makes
that the default for the whole suite instead of something each new test has to
remember. Tests that patch it themselves still win — an explicit
`patch.object` inside the test applies on top of this.
"""

import pytest

from backend.supervisor import tools


@pytest.fixture(autouse=True)
def _handoff_notes_go_to_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "HANDOFFS_DIR", tmp_path / "handoffs")
