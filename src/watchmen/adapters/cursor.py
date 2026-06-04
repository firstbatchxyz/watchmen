"""Cursor IDE session adapter — reads the SQLite chat store.

Cursor keeps its agent conversations in a VS Code-style key-value SQLite DB,
not in per-session JSON files. On macOS:

    ~/Library/Application Support/Cursor/User/globalStorage/state.vscdb

(see PLATFORM_DB_PATHS for Linux/Windows). The interesting table is
`cursorDiskKV`, a (key TEXT, value TEXT-JSON) store. Two key families matter:

  - composerData:<composerId>   — one row per conversation ("composer").
  - bubbleId:<composerId>:<bubbleId>  — one row per message, in the *modern*
    format where the composer only stores message headers.

Two conversation shapes exist and we handle both (Cursor migrated between them
and old rows survive):

  * Legacy: composerData has `conversation: [ <message>, ... ]` with the
    message bodies inlined.
  * Modern (`_v` present): composerData has
    `fullConversationHeadersOnly: [ {bubbleId, type}, ... ]` and each body
    lives in its own `bubbleId:<composerId>:<bubbleId>` row that we join in.

A message carries `type` (1 = user, 2 = assistant), `text`, optional
`thinking` (assistant reasoning), and optional `toolFormerData` (a tool call:
name + params, an outcome `status`, and a `result`/`additionalData` payload).

What this adapter can and cannot populate
-----------------------------------------
Cursor's store is conversation text + tool *invocations* + an outcome signal +
per-bubble `createdAt` timestamps. We populate:
  - `is_error` from `toolFormerData.status` ("error"/"cancelled"/… vs
    "completed"/"success"), `additionalData.status`, or a `userDecision ==
    "rejected"` on older builds. A user rejection counts as an error but carries
    NO friction signature — it's not the agent's recurring mistake — matching
    how the JSONL adapters treat rejections (#110).
  - started_at / ended_at / duration from the bubbles' `createdAt` ISO
    timestamps.
But NOT:
  - per-turn token usage or cost — the store's `tokenCount` is unreliable
    (commonly 0), so token columns and `cost_usd` stay 0. We do NOT estimate
    from a price table (per-skill cost attribution, #94, is unavailable).

project_dir comes from the conversation's `context.fileSelections[].uri.fsPath`
when present (the common workspace root of the files the chat touched); when the
conversation references no files we fall back to "(unknown)" rather than guess.

discover() walks every candidate DB (a user may have one global store and any
number of per-workspace `state.vscdb`s) and yields one entry per composer. The
DB is opened read-only so we never lock or mutate Cursor's live database.

NB: schema verified against the cursorDiskKV layout in a real Cursor install and
the field names used by mature community readers; tested with a synthetic DB
fixture that mirrors both the legacy and modern shapes. Cursor evolves this
schema fairly often, so every field access is defensive — an unfamiliar row
degrades to "skipped" rather than raising.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Iterable

from watchmen.adapters._shared import extract_skill_from_args, normalize_error_signature

NAME = "cursor"


def _parse_iso(ts):
    """Parse an ISO-8601 timestamp (tolerating a trailing 'Z'), else None."""
    if not isinstance(ts, str) or not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None

# Cursor's global chat store, per OS. The macOS path is the primary one; the
# others mirror VS Code's storage convention for Cursor. Resolved lazily in
# discover() so importing this module never touches the filesystem.
PLATFORM_DB_PATHS: tuple[Path, ...] = (
    # macOS
    Path.home() / "Library" / "Application Support" / "Cursor" / "User"
    / "globalStorage" / "state.vscdb",
    # Linux
    Path.home() / ".config" / "Cursor" / "User" / "globalStorage" / "state.vscdb",
)


def _windows_db_path() -> Path | None:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    return Path(appdata) / "Cursor" / "User" / "globalStorage" / "state.vscdb"


def _candidate_dbs() -> list[Path]:
    """Every state.vscdb worth reading: the per-OS global store plus any
    per-workspace stores under workspaceStorage/. De-duplicated, existing
    files only."""
    seen: dict[str, Path] = {}
    candidates = list(PLATFORM_DB_PATHS)
    win = _windows_db_path()
    if win:
        candidates.append(win)
    # Per-workspace stores sit next to the global one, under workspaceStorage/.
    for global_db in list(candidates):
        ws_root = global_db.parent.parent / "workspaceStorage"
        if ws_root.is_dir():
            candidates.extend(sorted(ws_root.glob("*/state.vscdb")))
    for db in candidates:
        try:
            if db.is_file():
                seen.setdefault(str(db.resolve()), db)
        except OSError:
            continue
    return list(seen.values())


def _connect(db_path: Path) -> sqlite3.Connection:
    """Open the store read-only so we never lock or mutate Cursor's live DB."""
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def _has_disk_kv(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("SELECT 1 FROM cursorDiskKV LIMIT 1")
        return True
    except sqlite3.OperationalError:
        return False


def discover() -> Iterable[dict]:
    for db_path in _candidate_dbs():
        try:
            conn = _connect(db_path)
        except sqlite3.OperationalError:
            continue
        try:
            if not _has_disk_kv(conn):
                continue
            composer_keys = conn.execute(
                "SELECT key FROM cursorDiskKV WHERE key LIKE 'composerData:%'"
            ).fetchall()
            # Also recover conversations whose composerData row is gone but whose
            # message bubbles survive: collect the composerId out of every
            # `bubbleId:<composerId>:<bubbleId>` key. scan() handles these via the
            # standalone-bubble path.
            bubble_keys = conn.execute(
                "SELECT DISTINCT key FROM cursorDiskKV WHERE key LIKE 'bubbleId:%'"
            ).fetchall()
        except sqlite3.OperationalError:
            continue
        finally:
            conn.close()
        composer_ids: list[str] = []
        seen_ids: set[str] = set()
        for (key,) in composer_keys:
            cid = key.split(":", 1)[1] if ":" in key else key
            if cid and cid not in seen_ids:
                seen_ids.add(cid)
                composer_ids.append(cid)
        for (key,) in bubble_keys:
            parts = key.split(":")
            cid = parts[1] if len(parts) >= 3 else None
            if cid and cid not in seen_ids:
                seen_ids.add(cid)
                composer_ids.append(cid)
        for composer_id in composer_ids:
            yield {
                # All composers in a DB share the file; scan_all keys incremental
                # skips on the DB's mtime, so the skip granularity is whole-DB —
                # fine for the handful of conversations Cursor holds.
                "path": db_path,
                "db_path": db_path,
                "composer_id": composer_id,
                "project_dir": None,  # derived from file context during scan
                "is_subagent": False,
                "parent_session_id": None,
            }


def _empty(session_id: str):
    return {
        "session_id": session_id,
        "project_dir": "(unknown)",
        "transcript_path": None,
        "started_at": None,
        "ended_at": None,
        "duration_seconds": None,
        "is_subagent": 0,
        "parent_session_id": None,
        "message_count": 0,
        "user_prompt_count": 0,
        "assistant_text_count": 0,
        "assistant_thinking_count": 0,
        "tool_use_count": 0,
        # Cursor's store has no tool result / failure flag.
        "tool_error_count": 0,
        "models": "[]",
        # No per-turn token usage or cost in the store; left at 0.
        "input_tokens": 0,
        "cache_creation_tokens": 0,
        "cache_read_tokens": 0,
        "output_tokens": 0,
        "model_dominant": None,
        "cost_usd": 0.0,
        "agent": NAME,
    }, [], []


def _project_dir_from_context(convo: dict) -> str | None:
    """The common workspace root of the files this conversation touched, taken
    from `context.fileSelections[].uri.fsPath`. Returns the shared parent dir of
    the referenced files, or the lone file's dir, or None when none are present."""
    ctx = convo.get("context")
    sels = ctx.get("fileSelections") if isinstance(ctx, dict) else None
    if not isinstance(sels, list):
        return None
    paths = []
    for sel in sels:
        uri = sel.get("uri") if isinstance(sel, dict) else None
        fs = uri.get("fsPath") if isinstance(uri, dict) else None
        if isinstance(fs, str) and fs:
            paths.append(fs)
    if not paths:
        return None
    try:
        common = os.path.commonpath(paths) if len(paths) > 1 else os.path.dirname(paths[0])
        return common or None
    except ValueError:
        # Mixed drives on Windows — no common path; fall back to the first dir.
        return os.path.dirname(paths[0]) or None


def _message_text(msg: dict) -> str:
    t = msg.get("text")
    return t if isinstance(t, str) else ""


def _message_ts(msg: dict) -> str | None:
    """An ISO-8601 timestamp if the bubble carries one. Verified against a real
    install: bubbles store `createdAt` like "2026-06-04T12:15:14.833Z"."""
    ts = msg.get("createdAt")
    return ts if isinstance(ts, str) and ts else None


def _tool_name(msg: dict) -> str | None:
    """A tool call is recorded in `toolFormerData`. Prefer the human-readable
    `name` (e.g. "run_terminal_command_v2", verified against a real install);
    fall back to other string fields. `tool` is a numeric tool-id in current
    builds, so we only use it as a last-resort stringified id."""
    tfd = msg.get("toolFormerData")
    if not isinstance(tfd, dict):
        return None
    for k in ("name", "toolName"):
        v = tfd.get(k)
        if isinstance(v, str) and v:
            return v
    tool = tfd.get("tool")
    if isinstance(tool, str) and tool:
        return tool
    if isinstance(tool, int):
        return f"tool_{tool}"  # numeric tool-id; no name table to resolve it
    return "?"  # tool call present but shape unfamiliar


def _tool_skill(msg: dict):
    """Skill detection: like the other non-Claude agents, a Cursor skill fires
    when the model reads its SKILL.md. Look through the tool call's params for a
    SKILL.md path using the shared heuristic."""
    tfd = msg.get("toolFormerData")
    if not isinstance(tfd, dict):
        return None
    for k in ("params", "rawArgs", "args", "input"):
        if k in tfd:
            args = tfd[k]
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except (json.JSONDecodeError, TypeError):
                    pass
            slug = extract_skill_from_args(args)
            if slug:
                return slug
    return None


def _tool_failure(msg: dict) -> tuple[int, str | None]:
    """Decide whether a tool call counts as a failure, and its error signature.

    `toolFormerData` carries the call's outcome. Verified against a real Cursor
    install, a completed call looks like:
        {"tool": 15, "name": "run_terminal_command_v2", "status": "completed",
         "params": "{...}", "result": "{\\"output\\": \\"...\\"}",
         "additionalData": {"status": "success", ...}}

    The outcome signal, in priority order:
      - `status` — "completed"/"success" is fine; "error"/"failed"/"cancelled"/
        "aborted"/"rejected" is a failure.
      - `additionalData.status` — secondary "success"/"error" flag.
      - `userDecision == "rejected"` — older builds recorded a user denial here.
        A user rejection counts as an error but carries NO friction signature
        (it's not the agent's recurring mistake).
      - an error-bearing `result` / `errorDetails` payload — folded into a
        signature for the friction ledger (#110).

    Returns (is_error, error_signature). Success → (0, None). Every access is
    defensive so an unfamiliar shape degrades to "success" rather than raising.
    """
    tfd = msg.get("toolFormerData")
    if not isinstance(tfd, dict):
        return 0, None

    _OK = {"completed", "success", "done", "finished"}
    _BAD = {"error", "errored", "failed", "failure", "cancelled", "canceled",
            "aborted", "rejected", "denied", "timeout", "timed_out"}

    def _status_of(d) -> str | None:
        s = d.get("status") if isinstance(d, dict) else None
        return s.lower() if isinstance(s, str) and s else None

    # User rejection (older builds): error, but not agent friction.
    decision = tfd.get("userDecision")
    if isinstance(decision, str) and decision.lower() == "rejected":
        return 1, None

    status = _status_of(tfd) or _status_of(tfd.get("additionalData"))
    if status in _OK:
        return 0, None
    if status in _BAD:
        # Genuine tool failure: fold the result/error text into a signature.
        payload = tfd.get("errorDetails") or tfd.get("result")
        sig = None
        if payload:
            sig = normalize_error_signature(
                payload if isinstance(payload, str) else json.dumps(payload)
            )
        return 1, sig

    # No usable status: fall back to inspecting an explicit error payload.
    err = tfd.get("errorDetails")
    if err:
        return 1, normalize_error_signature(
            err if isinstance(err, str) else json.dumps(err)
        )
    result = tfd.get("result")
    if isinstance(result, str) and result:
        low = result.replace(" ", "").lower()
        if '"error"' in low or '"rejected":true' in low:
            return 1, normalize_error_signature(result)
    return 0, None


def _iter_messages(conn: sqlite3.Connection, composer_id: str, convo: dict):
    """Yield each message dict for a conversation, handling both shapes.

    Legacy: bodies are inlined in `conversation`. Modern: `_v` is set and only
    `fullConversationHeadersOnly` (headers) live on the composer — each body is
    a separate `bubbleId:<composer>:<bubble>` row we fetch and join in.
    """
    legacy = convo.get("conversation")
    if isinstance(legacy, list):
        for m in legacy:
            if isinstance(m, dict):
                yield m
        return

    headers = convo.get("fullConversationHeadersOnly")
    if isinstance(headers, list) and headers:
        # Headers give us authoritative order + role; join each body in.
        for header in headers:
            if not isinstance(header, dict):
                continue
            bubble_id = header.get("bubbleId")
            if not isinstance(bubble_id, str) or not bubble_id:
                continue
            key = f"bubbleId:{composer_id}:{bubble_id}"
            try:
                row = conn.execute(
                    "SELECT value FROM cursorDiskKV WHERE key = ?", (key,)
                ).fetchone()
            except sqlite3.OperationalError:
                continue
            if not row:
                # Header without a body (pruned / not yet written) — fall back to
                # the header itself so the turn is still counted with its role.
                yield header
                continue
            try:
                msg = json.loads(row[0])
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(msg, dict):
                # The header carries the authoritative role; bodies sometimes
                # omit it.
                msg.setdefault("type", header.get("type"))
                yield msg
        return

    # No usable headers and no legacy `conversation`: the composerData row may be
    # an empty stub while the real messages live in standalone bubble rows. Scan
    # them directly, ordered by rowid (= insertion order, the only ordering the
    # store preserves). This is how mature community readers recover messages
    # when the composer header list is missing.
    try:
        rows = conn.execute(
            "SELECT value FROM cursorDiskKV WHERE key LIKE ? ORDER BY rowid",
            (f"bubbleId:{composer_id}:%",),
        ).fetchall()
    except sqlite3.OperationalError:
        return
    for (value,) in rows:
        try:
            msg = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(msg, dict):
            yield msg


def scan(entry: dict):
    db_path: Path = entry.get("db_path") or entry["path"]
    composer_id: str = entry["composer_id"]
    session, prompts, tool_calls = _empty(f"cursor/{composer_id}")
    session["transcript_path"] = str(db_path)

    try:
        conn = _connect(db_path)
    except sqlite3.OperationalError:
        return session, prompts, tool_calls
    try:
        row = conn.execute(
            "SELECT value FROM cursorDiskKV WHERE key = ?",
            (f"composerData:{composer_id}",),
        ).fetchone()
        # The composerData row may be absent (recovered via bubble keys) or
        # unparseable; in both cases fall through with an empty conversation so
        # `_iter_messages` recovers the standalone bubble rows for this composer.
        convo: dict = {}
        if row:
            try:
                parsed = json.loads(row[0])
                if isinstance(parsed, dict):
                    convo = parsed
            except (json.JSONDecodeError, TypeError):
                convo = {}

        project_dir = _project_dir_from_context(convo)
        if project_dir:
            session["project_dir"] = project_dir

        is_first_user = True
        for msg in _iter_messages(conn, composer_id, convo):
            mtype = msg.get("type")
            session["message_count"] += 1

            ts = _message_ts(msg)
            if ts:
                if not session["started_at"] or ts < session["started_at"]:
                    session["started_at"] = ts
                if not session["ended_at"] or ts > session["ended_at"]:
                    session["ended_at"] = ts

            if mtype == 1:  # user
                text = _message_text(msg)
                if not text.strip():
                    continue
                prompts.append({
                    "session_id": session["session_id"],
                    "timestamp": ts,
                    "text": text,
                    "word_count": len(text.split()),
                    "char_count": len(text),
                    "is_first_in_session": int(is_first_user),
                })
                is_first_user = False
                session["user_prompt_count"] += 1

            elif mtype == 2:  # assistant
                if _message_text(msg).strip():
                    session["assistant_text_count"] += 1
                thinking = msg.get("thinking")
                if (isinstance(thinking, str) and thinking.strip()) or isinstance(thinking, dict):
                    session["assistant_thinking_count"] += 1
                if isinstance(msg.get("toolFormerData"), dict):
                    session["tool_use_count"] += 1
                    is_error, error_sig = _tool_failure(msg)
                    if is_error:
                        session["tool_error_count"] += 1
                    tool_calls.append({
                        "session_id": session["session_id"],
                        "timestamp": ts,
                        "tool_name": _tool_name(msg) or "?",
                        "is_error": is_error,
                        "skill_name": _tool_skill(msg),
                        "error_signature": error_sig,
                    })
    finally:
        try:
            conn.close()
        except Exception:
            pass

    a, b = _parse_iso(session["started_at"]), _parse_iso(session["ended_at"])
    if a and b:
        session["duration_seconds"] = (b - a).total_seconds()
    return session, prompts, tool_calls
