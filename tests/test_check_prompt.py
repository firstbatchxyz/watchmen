"""Tests for the UserPromptSubmit suggestion hook (plugin/bin/check_prompt.py).

Focus: the anti-firehose dedup/cooldown added to stop a single skill being
re-suggested every prompt (observed 75x in one session). The hook is a
standalone script, so we load it by path and point its module-level paths at
tmp_path.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sqlite3
import time
from pathlib import Path

import pytest

HOOK_PATH = Path(__file__).resolve().parents[1] / "plugin" / "bin" / "check_prompt.py"


def _load_hook():
    spec = importlib.util.spec_from_file_location("watchmen_check_prompt", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def hook(tmp_path, monkeypatch):
    """The hook module with all of its filesystem paths redirected to tmp_path,
    a real repo dir, a projects.json that maps it to project_key 'proj', and a
    one-skill FTS5 index whose trigger matches the word 'deploy'."""
    mod = _load_hook()
    watchmen = tmp_path / ".watchmen"
    state = watchmen / "state"
    state.mkdir(parents=True)
    repo = tmp_path / "repo"
    repo.mkdir()

    index_db = watchmen / "skill_index.db"
    projects = watchmen / "projects.json"
    projects.write_text(json.dumps([{"source_repo": str(repo), "project_key": "proj"}]))

    with sqlite3.connect(str(index_db)) as conn:
        conn.execute(
            "CREATE VIRTUAL TABLE skill_match USING fts5("
            "when_to_use, when_not_to_use, skill_slug UNINDEXED, project_key UNINDEXED)"
        )
        conn.execute(
            "INSERT INTO skill_match (project_key, skill_slug, when_to_use, when_not_to_use) "
            "VALUES (?, ?, ?, ?)",
            ("proj", "railway-stack-provision",
             "deploy provision railway stack environment service", ""),
        )
        conn.commit()

    monkeypatch.setattr(mod, "WATCHMEN", watchmen)
    monkeypatch.setattr(mod, "INDEX_DB", index_db)
    monkeypatch.setattr(mod, "PROJECTS_INDEX", projects)
    monkeypatch.setattr(mod, "STATE_DIR", state)
    monkeypatch.setattr(mod, "SUGGESTIONS_LOG", watchmen / "suggestions.jsonl")
    # A one-row FTS5 table yields bm25 scores near 0 (IDF collapses on a tiny
    # corpus); the real 136-skill index scores well below -0.5. Use a permissive
    # bar here so the dedup tests exercise the dedup, not threshold calibration.
    # The threshold-specific test overrides this back to a strict value.
    monkeypatch.setattr(mod, "SCORE_THRESHOLD", 1.0)

    mod._repo = repo  # convenience for tests
    mod._state = state
    mod._log = watchmen / "suggestions.jsonl"
    return mod


def _run(hook, monkeypatch, prompt, session_id):
    evt = {
        "hook_event_name": "UserPromptSubmit",
        "prompt": prompt,
        "cwd": str(hook._repo),
        "session_id": session_id,
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(evt)))
    return hook.main()


def _suggestion(hook):
    f = hook._state / "proj.suggestion.json"
    return json.loads(f.read_text()) if f.exists() else None


def _log_lines(hook):
    if not hook._log.exists():
        return []
    return [json.loads(line) for line in hook._log.read_text().splitlines() if line.strip()]


def test_first_match_writes_suggestion_and_logs(hook, monkeypatch):
    rc = _run(hook, monkeypatch, "help me deploy and provision the railway stack", "s1")
    assert rc == 0
    sug = _suggestion(hook)
    assert sug and sug["skill_slug"] == "railway-stack-provision"
    assert len(_log_lines(hook)) == 1


def test_same_skill_same_session_suppressed(hook, monkeypatch):
    """The core firehose fix: the same skill matched repeatedly in one session
    is surfaced once, not once per prompt."""
    for _ in range(5):
        _run(hook, monkeypatch, "deploy provision railway stack again", "s1")
    # Only the first of the five wrote a log line / suggestion.
    assert len(_log_lines(hook)) == 1


def test_cooldown_suppresses_across_sessions(hook, monkeypatch):
    monkeypatch.setattr(hook, "SUGGEST_COOLDOWN_SECONDS", 6 * 3600)
    _run(hook, monkeypatch, "deploy provision railway stack", "s1")
    _run(hook, monkeypatch, "deploy provision railway stack", "s2")  # new session, within cooldown
    assert len(_log_lines(hook)) == 1


def test_cooldown_zero_allows_new_session(hook, monkeypatch):
    """Cooldown of 0 disables the cross-session layer; same-session dedup still
    holds, so a fresh session re-surfaces the skill once."""
    monkeypatch.setattr(hook, "SUGGEST_COOLDOWN_SECONDS", 0)
    _run(hook, monkeypatch, "deploy provision railway stack", "s1")
    _run(hook, monkeypatch, "deploy provision railway stack", "s2")
    assert len(_log_lines(hook)) == 2


def test_different_skill_still_suggested(hook, monkeypatch):
    """Dedup is per-skill, not a global mute: a second, distinct skill match
    still surfaces in the same session."""
    with sqlite3.connect(str(hook.INDEX_DB)) as conn:
        conn.execute(
            "INSERT INTO skill_match (project_key, skill_slug, when_to_use, when_not_to_use) "
            "VALUES (?, ?, ?, ?)",
            ("proj", "rerun-failed-cells", "rerun notebook failed cells kernel", ""),
        )
        conn.commit()
    _run(hook, monkeypatch, "deploy provision railway stack", "s1")
    _run(hook, monkeypatch, "rerun the failed notebook cells", "s1")
    slugs = {ln["skill_slug"] for ln in _log_lines(hook)}
    assert slugs == {"railway-stack-provision", "rerun-failed-cells"}


def test_threshold_env_configurable(hook, monkeypatch):
    """A stricter (more-negative) threshold rejects an otherwise-good match."""
    monkeypatch.setattr(hook, "SCORE_THRESHOLD", -1000.0)
    rc = _run(hook, monkeypatch, "deploy provision railway stack", "s1")
    assert rc == 0
    assert _suggestion(hook) is None
    assert _log_lines(hook) == []


def test_no_match_clears_prior_suggestion(hook, monkeypatch):
    """Unchanged behaviour: a prompt that matches nothing clears any standing
    suggestion so the statusline goes quiet."""
    _run(hook, monkeypatch, "deploy provision railway stack", "s1")
    assert _suggestion(hook) is not None
    _run(hook, monkeypatch, "what is the capital of France", "s1")
    assert _suggestion(hook) is None


def test_no_session_id_not_wedged_with_cooldown_zero(hook, monkeypatch):
    """With no session_id, prompts share the "?" bucket. A bare presence check
    would mute the skill forever; with cooldown=0 disabled, each prompt must
    still surface it (no permanent wedge)."""
    monkeypatch.setattr(hook, "SUGGEST_COOLDOWN_SECONDS", 0)
    _run(hook, monkeypatch, "deploy provision railway stack", None)
    _run(hook, monkeypatch, "deploy provision railway stack", None)
    assert len(_log_lines(hook)) == 2


def test_no_session_id_respects_cooldown_window(hook, monkeypatch):
    """No session_id falls through to the time-based cooldown: a numeric stamp
    inside the window suppresses, regardless of the "?" key."""
    now = time.time()
    monkeypatch.setattr(hook, "SUGGEST_COOLDOWN_SECONDS", 6 * 3600)
    seen = {"?|railway-stack-provision": now - 60}  # surfaced 60s ago, no session
    assert hook._recently_suggested(seen, None, "railway-stack-provision", now) is True
    # ...and once the window has elapsed, it's eligible again.
    old = {"?|railway-stack-provision": now - 7 * 3600}
    assert hook._recently_suggested(old, None, "railway-stack-provision", now) is False


def test_corrupt_seen_state_does_not_crash_prompt(hook, monkeypatch):
    """A corrupt/hand-edited seen-state file (non-numeric timestamp) must not
    crash the hook — it runs on every prompt, so a crash breaks submission.
    The hook fails open: it still surfaces the suggestion."""
    monkeypatch.setattr(hook, "SUGGEST_COOLDOWN_SECONDS", 6 * 3600)
    (hook._state / "proj.suggest_seen.json").write_text(
        json.dumps({"somesession|railway-stack-provision": "not-a-number"})
    )
    rc = _run(hook, monkeypatch, "deploy provision railway stack", "s-new")
    assert rc == 0
    assert _suggestion(hook) is not None  # fell open, surfaced rather than crashed


def test_recently_suggested_ignores_non_numeric_stamp(hook):
    """The cross-session cooldown scan skips non-numeric stamps instead of
    raising on the arithmetic."""
    now = time.time()
    seen = {"s1|railway-stack-provision": "bad", "s2|railway-stack-provision": now - 60}
    # Should not raise; the numeric stamp 60s ago is within the default cooldown.
    assert hook._recently_suggested(seen, "s3", "railway-stack-provision", now) is True


def test_recently_suggested_prunes_on_record(hook):
    """Records older than the TTL are dropped when a new one is written, so the
    seen-state file can't grow without bound."""
    now = time.time()
    seen = {"old|dead-skill": now - hook._SEEN_TTL_SECONDS - 10}
    hook._record_seen("proj", seen, "s1", "railway-stack-provision", now)
    persisted = json.loads((hook._state / "proj.suggest_seen.json").read_text())
    assert "old|dead-skill" not in persisted
    assert "s1|railway-stack-provision" in persisted
