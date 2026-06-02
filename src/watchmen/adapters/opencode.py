"""OpenCode agent session adapter — reads the SQLite store (opencode >= 1.x).

OpenCode 1.x dropped the per-session JSON files this adapter used to read and
moved to a Drizzle/SQLite store at ~/.local/share/opencode/opencode.db (path
also reported by `opencode db path`). Layout:

  - session: one row per session. Carries the AUTHORITATIVE rollups opencode
    computes itself — `cost`, `tokens_input/output/reasoning/cache_read/
    cache_write` — plus `parent_id` (subagent linkage), `agent`, `model`,
    `directory`, `time_created/updated`.
  - message: id, session_id, time_created, `data` (JSON = UserMessage |
    AssistantMessage; AssistantMessage carries per-message `cost` + `tokens`).
  - part: id, message_id, session_id, time_created, `data` (JSON Part —
    text / reasoning / tool / step-* ...).

Skills are a FIRST-CLASS `tool` part (`data.tool == "skill"`, slug in
`state.input.skillId`), not a SKILL.md read, so the path heuristic used by
the codex/pi adapters does not fire here — we key on the skill tool directly.

Cost: opencode prices everything itself. We take the session total from the
`session.cost` column and each AssistantMessage's `cost` (from message.data)
to attribute per-skill cost (#94), rather than recomputing from a price table.

NB: written against the opencode 1.15 SDK schema (@opencode-ai/sdk
types.gen.d.ts) + the live DB schema, and tested with a synthetic fixture DB.
Not yet validated against a real opencode session (none on the dev box;
generating one needs provider auth). Real-data validation is a follow-up.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from watchmen.adapters._shared import normalize_error_signature

NAME = "opencode"

DB_PATH = Path.home() / ".local" / "share" / "opencode" / "opencode.db"

_SESSION_COLS = (
    "id", "directory", "parent_id", "agent", "model", "cost",
    "tokens_input", "tokens_output", "tokens_reasoning",
    "tokens_cache_read", "tokens_cache_write", "time_created", "time_updated",
)


def _connect(db_path: Path) -> sqlite3.Connection:
    """Open the store read-only so we never lock or mutate the live DB."""
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def _epoch_to_iso(v) -> str | None:
    """opencode timestamps are epoch integers (milliseconds). Tolerate seconds
    too (heuristic: > 1e12 => ms) so a future unit change degrades gracefully."""
    if not isinstance(v, (int, float)) or v <= 0:
        return None
    seconds = v / 1000.0 if v > 1e12 else float(v)
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()
    except (OSError, ValueError, OverflowError):
        return None


def discover() -> Iterable[dict]:
    if not DB_PATH.exists():
        return
    try:
        conn = _connect(DB_PATH)
    except sqlite3.OperationalError:
        return
    try:
        rows = conn.execute(f"SELECT {', '.join(_SESSION_COLS)} FROM session").fetchall()
    except sqlite3.OperationalError:
        # Pre-1.x DB without these columns, or no session table — nothing to do.
        rows = []
    finally:
        conn.close()

    for r in rows:
        row = dict(zip(_SESSION_COLS, r))
        yield {
            # `path` must be a real, stat-able file: scan_all uses its mtime
            # for incremental skips. All sessions share the DB file, so the
            # skip granularity is whole-DB (re-scan every session when the DB
            # changes) — fine for the handful of sessions opencode holds.
            "path": DB_PATH,
            "db_path": DB_PATH,
            "session_id": row["id"],
            "session_row": row,
            "project_dir": row.get("directory") or "(unknown)",
            "is_subagent": bool(row.get("parent_id")),
            "parent_session_id": row.get("parent_id"),
        }


def _empty(session_id: str, project_dir: str | None):
    return {
        "session_id": session_id,
        "project_dir": project_dir or "(unknown)",
        "transcript_path": str(DB_PATH),
        "started_at": None, "ended_at": None, "duration_seconds": None,
        "is_subagent": 0, "parent_session_id": None,
        "message_count": 0, "user_prompt_count": 0,
        "assistant_text_count": 0, "assistant_thinking_count": 0,
        "tool_use_count": 0, "tool_error_count": 0,
        "models": "[]", "input_tokens": 0, "cache_creation_tokens": 0,
        "cache_read_tokens": 0, "output_tokens": 0,
        "model_dominant": None, "cost_usd": 0.0, "agent": NAME,
    }, [], []


def _skill_slug(state: dict) -> str | None:
    """A skill tool part carries the slug in state.input.skillId (the server
    also serializes it as skill_id in some paths — accept both)."""
    inp = state.get("input") if isinstance(state, dict) else None
    if isinstance(inp, dict):
        slug = inp.get("skillId") or inp.get("skill_id")
        if isinstance(slug, str) and slug.strip():
            return slug.strip()
    return None


def scan(entry: dict):
    db_path: Path = entry.get("db_path", DB_PATH)
    sid: str = entry["session_id"]
    row: dict = entry.get("session_row") or {}
    project_dir = entry.get("project_dir") or row.get("directory") or "(unknown)"

    session, prompts, tool_calls = _empty(sid, project_dir)
    session["is_subagent"] = int(bool(entry.get("parent_session_id") or row.get("parent_id")))
    session["parent_session_id"] = entry.get("parent_session_id") or row.get("parent_id")

    # Session-level rollups come straight from opencode's own columns.
    session["cost_usd"] = float(row.get("cost") or 0.0)
    session["input_tokens"] = int(row.get("tokens_input") or 0)
    session["output_tokens"] = int(row.get("tokens_output") or 0)
    session["cache_read_tokens"] = int(row.get("tokens_cache_read") or 0)
    session["cache_creation_tokens"] = int(row.get("tokens_cache_write") or 0)
    session["started_at"] = _epoch_to_iso(row.get("time_created"))
    session["ended_at"] = _epoch_to_iso(row.get("time_updated"))
    a, b = session["started_at"], session["ended_at"]
    if a and b:
        session["duration_seconds"] = (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds()

    try:
        conn = _connect(db_path)
    except sqlite3.OperationalError:
        return session, prompts, tool_calls
    try:
        msg_rows = conn.execute(
            "SELECT id, time_created, data FROM message WHERE session_id = ? "
            "ORDER BY time_created, id", (sid,),
        ).fetchall()
        part_rows = conn.execute(
            "SELECT message_id, time_created, data FROM part WHERE session_id = ? "
            "ORDER BY time_created, id", (sid,),
        ).fetchall()
    except sqlite3.OperationalError:
        return session, prompts, tool_calls
    finally:
        try:
            conn.close()
        except Exception:
            pass

    # Group parts under their message id.
    parts_by_msg: dict[str, list] = {}
    for mid, _p_ts, p_data in part_rows:
        parts_by_msg.setdefault(mid, []).append(p_data)

    models: set[str] = set()
    if row.get("model"):
        models.add(row["model"])
    is_first_user = True
    # Per-skill cost attribution (#94): a skill tool part opens a span;
    # subsequent messages' cost accrues into that skill's row until the next
    # skill or the next genuine user prompt. `active_skill` points at the row.
    active_skill: dict | None = None

    for mid, m_ts, m_data in msg_rows:
        try:
            msg = json.loads(m_data)
        except (json.JSONDecodeError, TypeError):
            continue
        session["message_count"] += 1
        role = msg.get("role")
        ts_iso = _epoch_to_iso(m_ts) or _epoch_to_iso((msg.get("time") or {}).get("created"))
        raw_parts = parts_by_msg.get(mid, [])

        if role == "user":
            text_parts = []
            for p_data in raw_parts:
                try:
                    part = json.loads(p_data)
                except (json.JSONDecodeError, TypeError):
                    continue
                if part.get("type") == "text" and not part.get("synthetic") and not part.get("ignored"):
                    if part.get("text"):
                        text_parts.append(part["text"])
            text = "\n".join(text_parts)
            if text:
                prompts.append({
                    "session_id": sid, "timestamp": ts_iso, "text": text,
                    "word_count": len(text.split()), "char_count": len(text),
                    "is_first_in_session": int(is_first_user),
                })
                is_first_user = False
                session["user_prompt_count"] += 1
                active_skill = None  # genuine prompt ends any skill span

        elif role == "assistant":
            if msg.get("modelID"):
                models.add(msg["modelID"])
            turn_cost = float(msg.get("cost") or 0.0)
            if active_skill is not None:
                active_skill["cost_usd"] = (active_skill["cost_usd"] or 0.0) + turn_cost
            for p_data in raw_parts:
                try:
                    part = json.loads(p_data)
                except (json.JSONDecodeError, TypeError):
                    continue
                ptype = part.get("type")
                if ptype == "text":
                    session["assistant_text_count"] += 1
                elif ptype == "reasoning":
                    session["assistant_thinking_count"] += 1
                elif ptype == "tool":
                    session["tool_use_count"] += 1
                    state = part.get("state") or {}
                    is_error = 1 if state.get("status") == "error" else 0
                    error_sig = None
                    if is_error:
                        session["tool_error_count"] += 1
                        # opencode keeps the failure message on the state; fold
                        # it into a signature for the friction ledger (#110).
                        error_sig = normalize_error_signature(
                            state.get("error") or state.get("output")
                        )
                    skill_name = _skill_slug(state) if part.get("tool") == "skill" else None
                    tc = {
                        "session_id": sid, "timestamp": ts_iso,
                        "tool_name": part.get("tool") or "?",
                        "is_error": is_error, "skill_name": skill_name,
                        "error_signature": error_sig,
                    }
                    if skill_name:
                        tc["cost_usd"] = 0.0
                        active_skill = tc
                    tool_calls.append(tc)

    session["models"] = json.dumps(sorted(models))
    session["model_dominant"] = row.get("model") or (sorted(models)[0] if models else None)
    return session, prompts, tool_calls
