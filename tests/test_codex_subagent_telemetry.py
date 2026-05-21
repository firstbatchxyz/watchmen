"""Codex 0.133.0+ subagent telemetry — session_meta.source parsing.

Before 0.133.0 the codex adapter had no way to tell whether a session was a
subagent or a top-level CLI run, so `is_subagent` was always 0. Codex 0.133.0
exposes lineage via SessionSource / SubAgentSource. These tests pin the
serialization shapes we depend on so a future codex release that changes the
schema fails loudly here.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from watchmen.adapters import codex
from watchmen.adapters.codex import _parse_session_source


def _scan_with_meta(payload: dict) -> dict:
    lines = [
        {"timestamp": "2026-05-21T12:00:00Z", "type": "session_meta", "payload": payload},
        {"timestamp": "2026-05-21T12:00:01Z", "type": "turn_context", "payload": {"model": "gpt-5"}},
    ]
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "rollout.jsonl"
        p.write_text("\n".join(json.dumps(line) for line in lines))
        session, _, _ = codex.scan({
            "path": p,
            "project_dir": None,
            "is_subagent": False,
            "parent_session_id": None,
        })
    return session


def test_parse_session_source_pre_0_133_string_is_main_session():
    # Pre-0.133.0 rollouts: `source` is a plain string ("cli", "vscode", ...).
    assert _parse_session_source("cli") == (0, None)
    assert _parse_session_source("vscode") == (0, None)
    assert _parse_session_source("exec") == (0, None)
    assert _parse_session_source("mcp") == (0, None)


def test_parse_session_source_thread_spawn_is_user_subagent_with_parent():
    src = {
        "subagent": {
            "thread_spawn": {
                "parent_thread_id": "11111111-aaaa-bbbb-cccc-222222222222",
                "depth": 1,
                "agent_role": "explore",
                "agent_nickname": None,
                "agent_path": None,
            }
        }
    }
    assert _parse_session_source(src) == (1, "11111111-aaaa-bbbb-cccc-222222222222")


def test_parse_session_source_internal_subagents_are_not_user_facing():
    # Review / Compact / MemoryConsolidation are codex's own bookkeeping turns.
    # They must NOT inflate user-facing subagent metrics.
    assert _parse_session_source({"subagent": "review"}) == (0, None)
    assert _parse_session_source({"subagent": "compact"}) == (0, None)
    assert _parse_session_source({"subagent": "memory_consolidation"}) == (0, None)


def test_parse_session_source_unknown_shape_falls_through_safely():
    # Anything we don't recognize degrades to "not a subagent" rather than raising,
    # so a future codex schema change doesn't break ingestion outright.
    assert _parse_session_source(None) == (0, None)
    assert _parse_session_source({"custom": "weirdo"}) == (0, None)
    assert _parse_session_source({"subagent": {"unknown_variant": {}}}) == (0, None)
    assert _parse_session_source({"subagent": {"thread_spawn": "not-a-dict"}}) == (0, None)


def test_codex_session_meta_without_source_marks_main_session():
    """Pre-0.133.0 rollouts may omit `source` entirely — must stay is_subagent=0."""
    session = _scan_with_meta({"id": "sess-pre-0133", "cwd": "/proj"})
    assert session["is_subagent"] == 0
    assert session["parent_session_id"] is None


def test_codex_session_meta_with_thread_spawn_marks_subagent_with_parent():
    parent_id = "33333333-cccc-dddd-eeee-444444444444"
    session = _scan_with_meta({
        "id": "child-sess-1",
        "cwd": "/proj",
        "source": {
            "subagent": {
                "thread_spawn": {
                    "parent_thread_id": parent_id,
                    "depth": 1,
                    "agent_role": "review-helper",
                }
            }
        },
    })
    assert session["is_subagent"] == 1
    assert session["parent_session_id"] == parent_id


def test_codex_session_meta_with_internal_subagent_stays_main_session():
    # Codex's Review/Compact turns must not show up in user-facing subagent share.
    session = _scan_with_meta({
        "id": "internal-review-sess",
        "cwd": "/proj",
        "source": {"subagent": "review"},
    })
    assert session["is_subagent"] == 0
    assert session["parent_session_id"] is None
