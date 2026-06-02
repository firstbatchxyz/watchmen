"""Error-signature capture for the friction ledger (#110).

Two layers, mirroring test_adapter_skill_attribution:

1. Unit tests on `normalize_error_signature` — the folding rules (paths,
   numbers, hashes, quotes), the special cases (Exit code prefix, Python
   traceback, multi-line shell output), the canonical collapses (denied
   bash, blocked sleep), and the None cases (empty, user cancel/reject,
   pure-punctuation).
2. Smoke tests that drive each adapter's `scan()` with a tiny transcript
   containing a failed tool call, asserting the row handed to the corpus
   writer is linked back (`is_error == 1`) and carries the expected
   `error_signature`.

If a future adapter stops linking results back to their call, the friction
ledger goes blind for that agent. The smoke tests are the canary.
"""

from __future__ import annotations

import json
from pathlib import Path

from watchmen.adapters import claude_code, codex, pi
from watchmen.adapters._shared import normalize_error_signature as n


# ── Unit: folding rules ──────────────────────────────────────────────────

def test_strips_tool_use_error_wrapper():
    assert n("<tool_use_error>String to replace not found in file.</tool_use_error>") == (
        "string to replace not found in file."
    )


def test_folds_paths_numbers_hashes_quotes():
    assert n("ls: /Users/x/agent/factory.py: No such file or directory") == (
        "ls: <path>: no such file or directory"
    )
    assert n("MCP error -32602: bad arguments") == "mcp error -<n>: bad arguments"
    assert n("opened webfetch-a1b2c3d4e5f6a7b8.pdf") == "opened webfetch<hex>.pdf"
    assert n('no module named "foo"') == "no module named <str>"


def test_exit_code_prefix_is_dropped():
    # "Exit code N" alone is a useless bucket; the line under it drives the sig.
    sig = n("Exit code 1\nfatal: not a git repository")
    assert sig == "fatal: not a git repository"


def test_bare_exit_code_folds_to_one_bucket():
    assert n("Exit code 1") == "shell command failed (exit code)"
    assert n("Exit code 127") == "shell command failed (exit code)"


def test_python_traceback_uses_exception_line():
    text = (
        "Exit code 1\nTraceback (most recent call last):\n"
        '  File "x.py", line 5, in <module>\n'
        "ModuleNotFoundError: No module named 'requests'"
    )
    assert n(text) == "modulenotfounderror: no module named <str>"


def test_multiline_shell_prefers_last_errorish_line():
    # stdout first, the actual failure trailing — pick the error, not line 1.
    text = "running tests...\nall queued\ncommand timed out after 10008 milliseconds"
    assert n(text) == "command timed out after <n> milliseconds"


def test_canonical_collapses_denied_bash_regardless_of_command():
    a = n("Permission to use Bash with command rm -rf /tmp/x has been denied.")
    b = n("Permission to use Bash with command git push --force has been denied.")
    assert a == b == "permission to use bash denied"


def test_canonical_collapses_blocked_sleep():
    sig = n("Blocked: sleep 60 followed by: gh pr checks 42. To wait, use Monitor.")
    assert sig == "blocked: sleep+command (use monitor to wait on a condition)"


# ── Unit: the None (non-friction) cases ──────────────────────────────────

def test_empty_returns_none():
    assert n("") is None
    assert n(None) is None
    assert n("   \n  ") is None


def test_user_cancel_and_reject_return_none():
    assert n("<tool_use_error>Cancelled: parallel tool call Bash(...) errored</tool_use_error>") is None
    assert n("The user doesn't want to proceed with this tool use. The tool use was rejected.") is None
    assert n("[Request interrupted by user]") is None
    assert n("<tool_use_error>Sibling tool call errored</tool_use_error>") is None


def test_pure_punctuation_returns_none():
    # A markdown separator that leaked out of stdout is not a mistake.
    assert n("---") is None
    assert n("===") is None


def test_accepts_list_of_content_blocks():
    # Claude Code tool_result content is sometimes [{type:text, text:...}].
    blocks = [{"type": "text", "text": "File has not been read yet. Read it first."}]
    assert n(blocks) == "file has not been read yet. read it first."


# ── Smoke: each adapter links the error back and signs it ────────────────

def _cc_transcript(tmp_path: Path) -> Path:
    """A Claude Code transcript: an assistant tool_use, then a user message
    carrying its errored tool_result (linked by id)."""
    lines = [
        {"type": "user", "timestamp": "2026-05-01T10:00:00Z",
         "message": {"role": "user", "content": "go"}},
        {"type": "assistant", "timestamp": "2026-05-01T10:00:01Z",
         "message": {"role": "assistant", "model": "claude", "id": "m1", "usage": {},
                     "content": [{"type": "tool_use", "id": "tu_1", "name": "Edit", "input": {}}]}},
        {"type": "user", "timestamp": "2026-05-01T10:00:02Z",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "tu_1", "is_error": True,
              "content": "<tool_use_error>File has not been read yet. Read it first before writing to it.</tool_use_error>"}]}},
    ]
    p = tmp_path / "cc.jsonl"
    p.write_text("\n".join(json.dumps(x) for x in lines))
    return p


def test_claude_code_links_and_signs_error(tmp_path):
    p = _cc_transcript(tmp_path)
    entry = {"path": p, "project_dir": "-x", "is_subagent": False, "parent_session_id": None}
    _sess, _prompts, tools = claude_code.scan(entry)
    edits = [t for t in tools if t["tool_name"] == "Edit"]
    assert len(edits) == 1
    assert edits[0]["is_error"] == 1
    assert edits[0]["error_signature"] == (
        "file has not been read yet. read it first before writing to it."
    )


def test_claude_code_user_cancel_is_error_without_signature(tmp_path):
    lines = [
        {"type": "assistant", "timestamp": "2026-05-01T10:00:01Z",
         "message": {"role": "assistant", "model": "claude", "id": "m1", "usage": {},
                     "content": [{"type": "tool_use", "id": "tu_9", "name": "Bash", "input": {}}]}},
        {"type": "user", "timestamp": "2026-05-01T10:00:02Z",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "tu_9", "is_error": True,
              "content": "The user doesn't want to proceed with this tool use. The tool use was rejected."}]}},
    ]
    p = tmp_path / "cc2.jsonl"
    p.write_text("\n".join(json.dumps(x) for x in lines))
    entry = {"path": p, "project_dir": "-x", "is_subagent": False, "parent_session_id": None}
    _sess, _prompts, tools = claude_code.scan(entry)
    bash = [t for t in tools if t["tool_name"] == "Bash"][0]
    assert bash["is_error"] == 1  # still an error for rate purposes
    assert bash.get("error_signature") is None  # but not friction → no signature


def test_codex_links_and_signs_nonzero_exit(tmp_path):
    lines = [
        {"type": "response_item", "timestamp": "2026-05-01T10:00:01Z",
         "payload": {"type": "function_call", "call_id": "c1", "name": "shell", "arguments": "{}"}},
        {"type": "response_item", "timestamp": "2026-05-01T10:00:02Z",
         "payload": {"type": "function_call_output", "call_id": "c1",
                     "output": json.dumps({"output": "fatal: not a git repository",
                                           "metadata": {"exit_code": 128}})}},
    ]
    p = tmp_path / "codex.jsonl"
    p.write_text("\n".join(json.dumps(x) for x in lines))
    entry = {"path": p, "project_dir": "codex", "is_subagent": False, "parent_session_id": None}
    _sess, _prompts, tools = codex.scan(entry)
    shell = [t for t in tools if t["tool_name"] == "shell"][0]
    assert shell["is_error"] == 1
    assert shell["error_signature"] == "fatal: not a git repository"


def test_codex_zero_exit_is_not_an_error(tmp_path):
    lines = [
        {"type": "response_item", "timestamp": "2026-05-01T10:00:01Z",
         "payload": {"type": "function_call", "call_id": "c2", "name": "shell", "arguments": "{}"}},
        {"type": "response_item", "timestamp": "2026-05-01T10:00:02Z",
         "payload": {"type": "function_call_output", "call_id": "c2",
                     "output": json.dumps({"output": "ok", "metadata": {"exit_code": 0}})}},
    ]
    p = tmp_path / "codex0.jsonl"
    p.write_text("\n".join(json.dumps(x) for x in lines))
    entry = {"path": p, "project_dir": "codex", "is_subagent": False, "parent_session_id": None}
    _sess, _prompts, tools = codex.scan(entry)
    shell = [t for t in tools if t["tool_name"] == "shell"][0]
    assert shell["is_error"] == 0
    assert shell.get("error_signature") is None


def test_pi_links_and_signs_error(tmp_path):
    lines = [
        {"type": "session", "version": 3, "id": "s-pi", "cwd": "/proj",
         "timestamp": "2026-05-01T10:00:00.000Z"},
        {"type": "message", "id": "a1", "parentId": "s-pi", "timestamp": "2026-05-01T10:00:01Z",
         "message": {"role": "assistant", "model": "claude", "content": [
             {"type": "toolCall", "id": "p1", "name": "edit", "arguments": {}}]}},
        {"type": "message", "id": "r1", "parentId": "a1", "timestamp": "2026-05-01T10:00:02Z",
         "message": {"role": "toolResult", "toolCallId": "p1", "toolName": "edit",
                     "isError": True, "content": "String to replace not found in file."}},
    ]
    p = tmp_path / "pi.jsonl"
    p.write_text("\n".join(json.dumps(x) for x in lines))
    entry = {"path": p, "project_dir": "--x--", "is_subagent": False, "parent_session_id": None}
    _sess, _prompts, tools = pi.scan(entry)
    edits = [t for t in tools if t["tool_name"] == "edit"]
    assert len(edits) == 1
    assert edits[0]["is_error"] == 1
    assert edits[0]["error_signature"] == "string to replace not found in file."
