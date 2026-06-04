"""Tests for watchmen.adapters.cursor — the Cursor SQLite chat-store parser.

Cursor keeps conversations in a VS Code-style key-value SQLite DB
(globalStorage/state.vscdb), table `cursorDiskKV`, in two shapes the adapter
must both handle:

  * legacy: composerData row with `conversation: [<message>, ...]` inlined.
  * modern (`_v` present): composerData row with
    `fullConversationHeadersOnly: [{bubbleId, type}]` and each message body in
    its own `bubbleId:<composer>:<bubble>` row.

These tests build a synthetic DB mirroring that layout and exercise discover()
+ scan(): both conversation shapes, user/assistant counting, thinking blocks,
tool calls from `toolFormerData`, skill detection from a SKILL.md path, the
project_dir derived from file context, and the two Cursor-specific facts the
adapter encodes — tool calls never carry an error and the session carries no
tokens/cost (the store has neither).
"""

import json
import sqlite3
from pathlib import Path

from watchmen.adapters import cursor


def _make_db(path: Path, rows: dict[str, dict]) -> None:
    """Create a state.vscdb-shaped DB. `rows` maps cursorDiskKV key -> JSON value."""
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)")
    conn.executemany(
        "INSERT INTO cursorDiskKV (key, value) VALUES (?, ?)",
        [(k, json.dumps(v)) for k, v in rows.items()],
    )
    conn.commit()
    conn.close()


def _discover(db: Path):
    """Point the adapter at exactly `db` (no per-OS / workspace fan-out)."""
    orig = cursor._candidate_dbs
    cursor._candidate_dbs = lambda: [db]
    try:
        return list(cursor.discover())
    finally:
        cursor._candidate_dbs = orig


def _scan(db: Path, composer_id: str):
    entry = next(e for e in _discover(db) if e["composer_id"] == composer_id)
    return cursor.scan(entry)


def test_discover_skips_when_no_disk_kv(tmp_path):
    """A DB without a cursorDiskKV table (or no composers) yields nothing."""
    db = tmp_path / "empty.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE ItemTable (key TEXT, value TEXT)")
    conn.commit()
    conn.close()
    assert _discover(db) == []


def test_discover_yields_one_entry_per_composer(tmp_path):
    db = tmp_path / "state.vscdb"
    _make_db(db, {
        "composerData:c1": {"_v": 14, "composerId": "c1", "fullConversationHeadersOnly": []},
        "composerData:c2": {"composerId": "c2", "conversation": []},
        "bubbleId:c1:b1": {"type": 1, "text": "noise, not a composer"},
    })
    entries = _discover(db)
    assert sorted(e["composer_id"] for e in entries) == ["c1", "c2"]


def test_scan_legacy_conversation(tmp_path):
    """Legacy shape: messages inlined in `conversation`. type 1=user, 2=assistant."""
    db = tmp_path / "state.vscdb"
    _make_db(db, {
        "composerData:leg": {
            "composerId": "leg",
            "conversation": [
                {"type": 1, "bubbleId": "b1", "text": "scan for ble devices"},
                {"type": 2, "bubbleId": "b2", "text": "Found 3 devices."},
                {"type": 1, "bubbleId": "b3", "text": "connect to the first"},
            ],
        },
    })
    session, prompts, tool_calls = _scan(db, "leg")

    assert session["agent"] == "cursor"
    assert session["session_id"] == "cursor/leg"
    assert session["user_prompt_count"] == 2
    assert session["assistant_text_count"] == 1
    assert session["tool_use_count"] == 0
    assert prompts[0]["text"] == "scan for ble devices"
    assert prompts[0]["is_first_in_session"] == 1
    assert prompts[1]["is_first_in_session"] == 0
    assert tool_calls == []


def test_scan_modern_joins_bubbles_with_tool_and_skill(tmp_path):
    """Modern shape: composer has headers only; bodies live in bubbleId rows.
    Covers a thinking block, a tool call (toolFormerData), skill detection from a
    SKILL.md path, and project_dir derived from file context."""
    db = tmp_path / "state.vscdb"
    _make_db(db, {
        "composerData:mod": {
            "_v": 14, "composerId": "mod",
            "context": {"fileSelections": [
                {"uri": {"fsPath": "/home/u/proj/src/a.py", "path": "/home/u/proj/src/a.py"}},
                {"uri": {"fsPath": "/home/u/proj/src/b.py", "path": "/home/u/proj/src/b.py"}},
            ]},
            "fullConversationHeadersOnly": [
                {"bubbleId": "b1", "type": 1},
                {"bubbleId": "b2", "type": 2},
                {"bubbleId": "b3", "type": 2},
            ],
        },
        "bubbleId:mod:b1": {"type": 1, "text": "run the ble scan skill"},
        # assistant turn with thinking + a tool call that reads a SKILL.md
        "bubbleId:mod:b2": {
            "type": 2, "text": "On it.", "thinking": "I should read the skill",
            "toolFormerData": {"name": "read_file",
                               "params": {"path": "/repo/.cursor/skills/ble-scan/SKILL.md"}},
        },
        # assistant turn with a plain tool call (no skill)
        "bubbleId:mod:b3": {
            "type": 2, "text": "",
            "toolFormerData": {"name": "run_terminal_cmd",
                               "rawArgs": "{\"command\": \"exit 1\"}"},
        },
    })
    session, prompts, tool_calls = _scan(db, "mod")

    assert session["user_prompt_count"] == 1
    assert session["assistant_text_count"] == 1          # b2 has text, b3 is empty
    assert session["assistant_thinking_count"] == 1      # b2 thinking
    assert session["tool_use_count"] == 2
    # project_dir is the common root of the two referenced files
    assert session["project_dir"] == "/home/u/proj/src"
    assert prompts[0]["text"] == "run the ble scan skill"

    skill_rows = [t for t in tool_calls if t["skill_name"]]
    assert len(skill_rows) == 1
    assert skill_rows[0]["tool_name"] == "read_file"
    assert skill_rows[0]["skill_name"] == "ble-scan"


def test_accepted_tool_call_is_not_an_error_and_no_cost(tmp_path):
    """A normal (accepted / undecided) tool call is is_error=0, and Cursor's
    store carries no token/cost, so those stay zero. Pin the no-cost fact down."""
    db = tmp_path / "state.vscdb"
    _make_db(db, {
        "composerData:c": {"composerId": "c", "conversation": [
            {"type": 2, "bubbleId": "b1", "text": "",
             "toolFormerData": {"name": "run_terminal_cmd",
                                "params": "{\"command\": \"ls\"}",
                                "userDecision": "accepted"}},
        ]},
    })
    session, _prompts, tool_calls = _scan(db, "c")
    assert tool_calls[0]["is_error"] == 0
    assert tool_calls[0]["skill_name"] is None
    assert session["tool_error_count"] == 0
    assert session["cost_usd"] == 0.0
    assert session["input_tokens"] == 0 and session["output_tokens"] == 0


def test_rejected_tool_call_counts_as_error_without_friction_signature(tmp_path):
    """`userDecision == "rejected"` is a user-driven denial: it counts toward the
    error rate but carries no friction signature (not the agent's recurring
    mistake), matching how the JSONL adapters treat rejections."""
    db = tmp_path / "state.vscdb"
    _make_db(db, {
        "composerData:c": {"composerId": "c", "conversation": [
            {"type": 2, "bubbleId": "b1", "text": "",
             "toolFormerData": {"name": "run_terminal_cmd",
                                "params": "{\"command\":\"rm -rf /\"}",
                                "userDecision": "rejected",
                                "result": "{\"rejected\":true}"}},
        ]},
    })
    session, _prompts, tool_calls = _scan(db, "c")
    assert tool_calls[0]["is_error"] == 1
    assert tool_calls[0]["error_signature"] is None
    assert session["tool_error_count"] == 1


def test_tool_call_with_error_result_gets_friction_signature(tmp_path):
    """An error-bearing `errorDetails` payload is folded into a friction
    signature for the ledger (#110)."""
    db = tmp_path / "state.vscdb"
    _make_db(db, {
        "composerData:c": {"composerId": "c", "conversation": [
            {"type": 2, "bubbleId": "b1", "text": "",
             "toolFormerData": {"name": "edit_file",
                                "params": "{}",
                                "errorDetails": "Error: file not found: /tmp/x.py"}},
        ]},
    })
    session, _prompts, tool_calls = _scan(db, "c")
    assert tool_calls[0]["is_error"] == 1
    assert tool_calls[0]["error_signature"]  # non-empty, folded
    assert session["tool_error_count"] == 1


def test_scan_handles_missing_bubble_body_gracefully(tmp_path):
    """A header whose bubbleId row is absent (pruned) must not crash — the turn
    still counts via the header's role."""
    db = tmp_path / "state.vscdb"
    _make_db(db, {
        "composerData:c": {"_v": 14, "composerId": "c", "fullConversationHeadersOnly": [
            {"bubbleId": "present", "type": 1},
            {"bubbleId": "missing", "type": 2},
        ]},
        "bubbleId:c:present": {"type": 1, "text": "hello"},
        # no bubbleId:c:missing row
    })
    session, prompts, _tool_calls = _scan(db, "c")
    assert session["user_prompt_count"] == 1
    assert prompts[0]["text"] == "hello"
    assert session["message_count"] == 2  # both headers counted, no crash


def test_discover_recovers_composer_from_bubble_keys_only(tmp_path):
    """A conversation whose composerData row is gone but whose bubble rows
    survive must still be discovered (composerId recovered from the key)."""
    db = tmp_path / "state.vscdb"
    _make_db(db, {
        "bubbleId:orphan:b1": {"type": 1, "text": "still here"},
        "bubbleId:orphan:b2": {"type": 2, "text": "and the reply"},
    })
    entries = _discover(db)
    assert [e["composer_id"] for e in entries] == ["orphan"]


def test_scan_recovers_messages_from_standalone_bubbles(tmp_path):
    """No composerData (or an empty stub): messages are read straight from the
    bubble rows, ordered by insertion (rowid)."""
    db = tmp_path / "state.vscdb"
    _make_db(db, {
        # empty stub composer — no conversation, no headers
        "composerData:c": {"_v": 14, "composerId": "c", "fullConversationHeadersOnly": []},
        "bubbleId:c:b1": {"type": 1, "text": "first user msg"},
        "bubbleId:c:b2": {"type": 2, "text": "assistant reply"},
        "bubbleId:c:b3": {"type": 1, "text": "second user msg"},
    })
    session, prompts, _tool_calls = _scan(db, "c")
    assert session["user_prompt_count"] == 2
    assert session["assistant_text_count"] == 1
    assert [p["text"] for p in prompts] == ["first user msg", "second user msg"]


def test_scan_unknown_project_when_no_file_context(tmp_path):
    db = tmp_path / "state.vscdb"
    _make_db(db, {
        "composerData:c": {"composerId": "c", "conversation": [
            {"type": 1, "bubbleId": "b1", "text": "hi"}]},
    })
    session, _prompts, _tool_calls = _scan(db, "c")
    assert session["project_dir"] == "(unknown)"
