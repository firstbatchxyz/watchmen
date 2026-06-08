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
import os
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
    """Point the IDE-store source at exactly `db` (no per-OS / workspace
    fan-out, and no agent-transcript entries from the machine running the
    tests)."""
    orig = cursor._candidate_dbs
    cursor._candidate_dbs = lambda: [db]
    try:
        return list(cursor._discover_ide_store())
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
    # project_dir is the common root of the two referenced files. The adapter
    # derives it via os.path.commonpath, which uses the host's separator — "\" on
    # Windows — so compare against an os.sep-normalized path rather than a literal.
    assert session["project_dir"] == os.path.normpath("/home/u/proj/src")
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


def test_scan_real_schema_status_name_timestamp(tmp_path):
    """Mirrors the toolFormerData shape verified against a real Cursor install:
    `name` is the human tool name, `tool` is a numeric id, `status` is the
    outcome, bubbles carry `createdAt`. A completed call is not an error, and
    started_at/ended_at/duration come from createdAt."""
    db = tmp_path / "state.vscdb"
    _make_db(db, {
        "composerData:r": {"composerId": "r", "conversation": [
            {"type": 1, "bubbleId": "b1", "text": "list the files",
             "createdAt": "2026-06-04T12:16:51.414Z"},
            {"type": 2, "bubbleId": "b2", "text": "Listing.",
             "createdAt": "2026-06-04T12:16:55.000Z"},
            {"type": 2, "bubbleId": "b3", "text": "",
             "createdAt": "2026-06-04T12:17:45.295Z",
             "toolFormerData": {
                 "tool": 15, "name": "run_terminal_command_v2", "status": "completed",
                 "params": "{\"command\":\"ls -la\"}",
                 "result": "{\"output\":\"total 0\\n...\"}",
                 "additionalData": {"status": "success"}}},
        ]},
    })
    session, prompts, tool_calls = _scan(db, "r")
    assert session["tool_use_count"] == 1
    assert tool_calls[0]["tool_name"] == "run_terminal_command_v2"  # name, not tool id
    assert tool_calls[0]["is_error"] == 0                            # status completed
    assert session["tool_error_count"] == 0
    assert prompts[0]["timestamp"] == "2026-06-04T12:16:51.414Z"
    assert session["started_at"] == "2026-06-04T12:16:51.414Z"
    assert session["ended_at"] == "2026-06-04T12:17:45.295Z"
    assert session["duration_seconds"] and session["duration_seconds"] > 50


def test_scan_status_error_is_failure_with_signature(tmp_path):
    """A toolFormerData.status of "error" (real outcome value) marks the call as
    a failure and folds the result into a friction signature (#110)."""
    db = tmp_path / "state.vscdb"
    _make_db(db, {
        "composerData:e": {"composerId": "e", "conversation": [
            {"type": 2, "bubbleId": "b1", "text": "",
             "toolFormerData": {
                 "tool": 15, "name": "run_terminal_command_v2", "status": "error",
                 "params": "{\"command\":\"badcmd\"}",
                 "result": "{\"output\":\"zsh: command not found: badcmd\"}"}},
        ]},
    })
    session, _prompts, tool_calls = _scan(db, "e")
    assert tool_calls[0]["is_error"] == 1
    assert tool_calls[0]["error_signature"]  # folded from result
    assert session["tool_error_count"] == 1


def test_numeric_tool_id_falls_back_to_tool_n(tmp_path):
    """If only the numeric `tool` id is present (no name), surface it as
    `tool_<n>` rather than dropping the call's identity."""
    db = tmp_path / "state.vscdb"
    _make_db(db, {
        "composerData:n": {"composerId": "n", "conversation": [
            {"type": 2, "bubbleId": "b1", "text": "",
             "toolFormerData": {"tool": 7, "status": "completed"}},
        ]},
    })
    _session, _prompts, tool_calls = _scan(db, "n")
    assert tool_calls[0]["tool_name"] == "tool_7"


def test_model_from_composer_modelconfig(tmp_path):
    """The conversation-level model comes from `composerData.modelConfig`, the
    authoritative source verified against a real install. A concrete pick passes
    through verbatim into models / model_dominant; tokens/cost stay zero (the
    model signal is the NAME only, #113 follow-up)."""
    db = tmp_path / "state.vscdb"
    _make_db(db, {
        "composerData:c": {
            "composerId": "c",
            "modelConfig": {"modelName": "composer-2.5", "maxMode": False,
                            "selectedModels": [{"modelId": "composer-2.5"}]},
            "conversation": [
                {"type": 1, "bubbleId": "b1", "text": "hi",
                 "modelInfo": {"modelName": "composer-2.5"}},
                {"type": 2, "bubbleId": "b2", "text": "hello"},
            ],
        },
    })
    session, _prompts, _tool_calls = _scan(db, "c")
    assert session["models"] == json.dumps(["composer-2.5"])
    assert session["model_dominant"] == "composer-2.5"
    # name only — no tokens/cost leak in.
    assert session["input_tokens"] == 0 and session["output_tokens"] == 0
    assert session["cost_usd"] == 0.0


def test_auto_mode_default_normalizes_to_auto(tmp_path):
    """Under Cursor's Auto mode the store records the literal "default" (verified
    against a real install — not blank, not "auto"). We normalize it to "auto"
    so the corpus carries an explicit Auto signal."""
    db = tmp_path / "state.vscdb"
    _make_db(db, {
        "composerData:c": {
            "composerId": "c",
            "modelConfig": {"modelName": "default", "maxMode": False},
            "conversation": [{"type": 1, "bubbleId": "b1", "text": "hi"}],
        },
    })
    session, _prompts, _tool_calls = _scan(db, "c")
    assert session["models"] == json.dumps(["auto"])
    assert session["model_dominant"] == "auto"


def test_model_falls_back_to_bubble_modelinfo(tmp_path):
    """When the composerData row is gone (recovered from standalone bubbles), the
    model is recovered from each user bubble's `modelInfo.modelName` — the field
    community readers (CodeBurn) read."""
    db = tmp_path / "state.vscdb"
    _make_db(db, {
        # no composerData row at all — recovered via bubble keys
        "bubbleId:orphan:b1": {"type": 1, "text": "first",
                               "modelInfo": {"modelName": "composer-2.5"}},
        "bubbleId:orphan:b2": {"type": 2, "text": "reply"},
        "bubbleId:orphan:b3": {"type": 1, "text": "second",
                               "modelInfo": {"modelName": "composer-2.5"}},
    })
    session, _prompts, _tool_calls = _scan(db, "orphan")
    assert session["models"] == json.dumps(["composer-2.5"])
    assert session["model_dominant"] == "composer-2.5"


def test_model_dominant_is_most_frequent(tmp_path):
    """No per-model tokens in the store, so model_dominant is the MOST FREQUENT
    model (not token-weighted like pi). Here two bubbles use model-b vs the
    composer pick + one bubble on model-a, so model-b wins on count."""
    db = tmp_path / "state.vscdb"
    _make_db(db, {
        "composerData:c": {
            "composerId": "c",
            "modelConfig": {"modelName": "model-a"},
            "conversation": [
                {"type": 1, "bubbleId": "b1", "text": "q1",
                 "modelInfo": {"modelName": "model-b"}},
                {"type": 1, "bubbleId": "b2", "text": "q2",
                 "modelInfo": {"modelName": "model-b"}},
            ],
        },
    })
    session, _prompts, _tool_calls = _scan(db, "c")
    # model-a: 1 (composer), model-b: 2 (bubbles) -> sorted list, b dominant
    assert session["models"] == json.dumps(["model-a", "model-b"])
    assert session["model_dominant"] == "model-b"


def test_no_model_leaves_fields_empty(tmp_path):
    """A conversation with no modelConfig and no bubble modelInfo keeps the
    original empty default (models="[]", model_dominant=None) — we don't invent
    a model where the store records none."""
    db = tmp_path / "state.vscdb"
    _make_db(db, {
        "composerData:c": {"composerId": "c", "conversation": [
            {"type": 1, "bubbleId": "b1", "text": "hi"}]},
    })
    session, _prompts, _tool_calls = _scan(db, "c")
    assert session["models"] == "[]"
    assert session["model_dominant"] is None


def test_scan_unknown_project_when_no_file_context(tmp_path):
    db = tmp_path / "state.vscdb"
    _make_db(db, {
        "composerData:c": {"composerId": "c", "conversation": [
            {"type": 1, "bubbleId": "b1", "text": "hi"}]},
    })
    session, _prompts, _tool_calls = _scan(db, "c")
    assert session["project_dir"] == "(unknown)"


# ─── agent-transcript JSONL source ──────────────────────────────────────────
#
# The second store the adapter reads:
# ~/.cursor/projects/<slug>/agent-transcripts/<sid>/<sid>.jsonl — role/message
# lines with Claude-API-shaped content blocks, the user's prompt wrapped in
# <user_query>, editor-injected context in sibling tags, and the only clock
# being the human-format <timestamp> tag on user messages.

TRANSCRIPT_FIXTURE = Path(__file__).parent / "fixtures" / "cursor_session.jsonl"


def _entry(path: Path, project_dir: str = "/home/u/repos/proj") -> dict:
    return {
        "path": path,
        "project_dir": project_dir,
        "is_subagent": False,
        "parent_session_id": None,
    }


def test_parse_human_timestamp_am_positive_offset():
    assert cursor._parse_human_timestamp("Tuesday, Apr 28, 2026, 10:15 AM (UTC+3)") == "2026-04-28T07:15:00+00:00"


def test_parse_human_timestamp_pm_and_noon_midnight():
    # PM adds 12; 12 AM is midnight, 12 PM is noon.
    assert cursor._parse_human_timestamp("Friday, Jun 5, 2026, 1:01 PM (UTC+3)") == "2026-06-05T10:01:00+00:00"
    assert cursor._parse_human_timestamp("Friday, Jun 5, 2026, 12:00 AM (UTC+0)") == "2026-06-05T00:00:00+00:00"
    assert cursor._parse_human_timestamp("Friday, Jun 5, 2026, 12:00 PM (UTC+0)") == "2026-06-05T12:00:00+00:00"


def test_parse_human_timestamp_negative_and_fractional_offsets():
    assert cursor._parse_human_timestamp("Monday, May 4, 2026, 9:30 AM (UTC-5)") == "2026-05-04T14:30:00+00:00"
    assert cursor._parse_human_timestamp("Monday, May 4, 2026, 9:30 AM (UTC+5:30)") == "2026-05-04T04:00:00+00:00"


def test_parse_human_timestamp_rejects_garbage():
    assert cursor._parse_human_timestamp("") is None
    assert cursor._parse_human_timestamp("not a timestamp") is None
    assert cursor._parse_human_timestamp("Tuesday, Foo 28, 2026, 10:15 AM (UTC+3)") is None


def test_scan_transcript_fixture():
    session, prompts, tool_calls = cursor.scan(_entry(TRANSCRIPT_FIXTURE))

    assert session["session_id"] == "cursor_session"
    assert session["agent"] == "cursor"
    assert session["project_dir"] == "/home/u/repos/proj"
    assert session["message_count"] == 5
    assert session["user_prompt_count"] == 2
    assert session["assistant_text_count"] == 2
    # Transcripts fold thinking into plain text blocks — never counted
    # separately (unlike the IDE store's `thinking` field).
    assert session["assistant_thinking_count"] == 0
    # 3 real tool_use blocks + 1 manually-attached-skill pseudo `Skill` call.
    assert session["tool_use_count"] == 4
    # No tool results / usage in the transcript format → defaults.
    assert session["tool_error_count"] == 0
    assert session["models"] == "[]"
    assert session["input_tokens"] == 0
    assert session["cost_usd"] == 0.0

    # Timestamps normalized to UTC ISO; duration spans first→last prompt.
    assert session["started_at"] == "2026-04-28T07:15:00+00:00"
    assert session["ended_at"] == "2026-04-28T10:01:00+00:00"
    assert session["duration_seconds"] == 9960.0


def test_transcript_extracts_user_queries_not_injected_context():
    _, prompts, _ = cursor.scan(_entry(TRANSCRIPT_FIXTURE))

    # The <attached_files>-only message must NOT become a prompt.
    assert len(prompts) == 2
    assert prompts[0]["text"] == "Why is the login failing in CI?"
    assert prompts[0]["is_first_in_session"] == 1
    assert prompts[0]["timestamp"] == "2026-04-28T07:15:00+00:00"
    assert prompts[1]["text"] == "Now respond to the review comments"
    assert prompts[1]["is_first_in_session"] == 0
    assert prompts[1]["timestamp"] == "2026-04-28T10:01:00+00:00"


def test_transcript_tool_calls_and_skill_attribution():
    _, _, tool_calls = cursor.scan(_entry(TRANSCRIPT_FIXTURE))

    assert [tc["tool_name"] for tc in tool_calls] == ["ReadFile", "Shell", "Skill", "Grep"]
    # SKILL.md read by path → slug via the shared extractor.
    assert tool_calls[0]["skill_name"] == "test-runner"
    assert tool_calls[1]["skill_name"] is None
    # Manually attached skill → pseudo `Skill` call, same convention as the
    # Claude Code adapter's first-class Skill tool.
    assert tool_calls[2]["skill_name"] == "coderabbit-respond"
    assert tool_calls[2]["timestamp"] == "2026-04-28T10:01:00+00:00"
    # Assistant turns carry no clock; tool calls inherit the prompt's stamp.
    assert tool_calls[3]["timestamp"] == "2026-04-28T10:01:00+00:00"
    assert all(tc["is_error"] == 0 for tc in tool_calls)


def test_transcript_without_timestamp_tags_falls_back_to_mtime(tmp_path):
    """Older Cursor builds didn't inject <timestamp> tags; the session should
    land on the file-mtime day instead of being dateless."""
    p = tmp_path / "old.jsonl"
    p.write_text(json.dumps({"role": "user", "message": {"content": [{"type": "text", "text": "<user_query>hi</user_query>"}]}}) + "\n")
    os.utime(p, (1_745_000_000, 1_745_000_000))  # 2025-04-18T18:13:20Z

    session, prompts, _ = cursor.scan(_entry(p))
    assert session["started_at"] == session["ended_at"] == "2025-04-18T18:13:20+00:00"
    assert session["duration_seconds"] == 0.0
    # Prompt rows never borrow mtime — only the session-level bounds do.
    assert prompts[0]["timestamp"] is None


def test_transcript_empty_or_corrupt_file(tmp_path):
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    session, prompts, tool_calls = cursor.scan(_entry(empty))
    assert session["agent"] == "cursor"
    assert session["message_count"] == 0
    assert prompts == [] and tool_calls == []

    corrupt = tmp_path / "corrupt.jsonl"
    corrupt.write_text("not json\n" + json.dumps({"role": "user", "message": {"content": [{"type": "text", "text": "<user_query>hi</user_query>"}]}}) + "\n")
    session, prompts, _ = cursor.scan(_entry(corrupt))
    # Bad lines skipped, good lines still parsed.
    assert session["message_count"] == 1
    assert len(prompts) == 1
    assert prompts[0]["timestamp"] is None  # no <timestamp> tag anywhere


def test_discover_walks_agent_transcripts(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    # Real session layout: <slug>/agent-transcripts/<sid>/<sid>.jsonl
    sid = "0a9a0a9e-3632-4c79-acbc-6cdf354e6312"
    sess_dir = projects / "home-user-myproj" / "agent-transcripts" / sid
    sess_dir.mkdir(parents=True)
    (sess_dir / f"{sid}.jsonl").write_text("")
    # A project dir without agent-transcripts must be skipped.
    (projects / "home-user-other" / "rules").mkdir(parents=True)
    # Stray non-dir entries inside agent-transcripts must be skipped.
    (projects / "home-user-myproj" / "agent-transcripts" / "stray.txt").write_text("x")

    monkeypatch.setattr(cursor, "PROJECTS_DIR", projects)
    entries = list(cursor._discover_agent_transcripts())

    assert len(entries) == 1
    assert entries[0]["path"].name == f"{sid}.jsonl"
    # Slug decodes like Claude Code's encoding minus the leading dash; the
    # path doesn't exist on this machine so the naive fallback applies.
    assert entries[0]["project_dir"] == "/home/user/myproj"
    assert entries[0]["is_subagent"] is False
    assert entries[0]["parent_session_id"] is None


def test_discover_missing_transcript_install_is_silent(tmp_path, monkeypatch):
    monkeypatch.setattr(cursor, "PROJECTS_DIR", tmp_path / "nope")
    assert list(cursor._discover_agent_transcripts()) == []


# ─── both sources behind one NAME ───────────────────────────────────────────


def test_discover_concatenates_both_sources_and_scan_dispatches(tmp_path, monkeypatch):
    """discover() yields IDE composers then transcript files; scan() routes on
    the entry shape (composer_id present → IDE store)."""
    # One composer in a synthetic IDE store…
    db = tmp_path / "state.vscdb"
    _make_db(db, {
        "composerData:c1": {"conversation": [
            {"type": 1, "text": "hello from the IDE"},
        ]},
    })
    monkeypatch.setattr(cursor, "_candidate_dbs", lambda: [db])
    # …and one agent transcript.
    sid = "11111111-2222-3333-4444-555555555555"
    sess_dir = tmp_path / "projects" / "home-user-proj" / "agent-transcripts" / sid
    sess_dir.mkdir(parents=True)
    (sess_dir / f"{sid}.jsonl").write_text(
        json.dumps({"role": "user", "message": {"content": [{"type": "text", "text": "<user_query>hello from the agent</user_query>"}]}}) + "\n"
    )
    monkeypatch.setattr(cursor, "PROJECTS_DIR", tmp_path / "projects")

    entries = list(cursor.discover())
    assert len(entries) == 2
    composer = next(e for e in entries if "composer_id" in e)
    transcript = next(e for e in entries if "composer_id" not in e)

    s1, p1, _ = cursor.scan(composer)
    assert s1["session_id"] == "cursor/c1"
    assert p1[0]["text"] == "hello from the IDE"

    s2, p2, _ = cursor.scan(transcript)
    assert s2["session_id"] == sid
    assert p2[0]["text"] == "hello from the agent"

    # Same agent slug from both sources, disjoint id spaces.
    assert s1["agent"] == s2["agent"] == "cursor"
