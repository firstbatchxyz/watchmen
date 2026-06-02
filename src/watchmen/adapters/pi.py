"""pi.dev (Pi Coding Agent) session adapter — format v3.

Reads ~/.pi/agent/sessions/--<encoded-cwd>--/<ts>_<uuid>.jsonl. Each line is
one entry; entries form a tree via id + parentId (branching lives in-file,
unlike Claude Code where forks become separate files).

Header entry:
    {"type":"session","version":3,"id":"<sid>","timestamp":"<iso>","cwd":"/proj"}

Message entry (the wrapper):
    {"type":"message","id":"<id>","parentId":"<pid>","timestamp":"<iso>",
     "message":{"role":"<role>","content":[...],"provider":"...","model":"...","usage":{...}}}

Roles we see in `message.role`:
  user / assistant / toolResult / bashExecution / custom / branchSummary / compactionSummary

Compaction entries (top-level type=compaction) carry firstKeptEntryId — entries
in the active walk that come BEFORE that id are summarized away and shouldn't
be re-ingested. We honor the cutoff.

Active branch selection: the spec doesn't store a "head" pointer. We pick the
leaf (entry with no children in the file) with the latest timestamp. Works
when there's one obvious tip; ambiguous mid-edit branching just picks the
most recently-touched line, which matches user intent in practice.

Cost: pi's assistant `usage` carries a precomputed `cost` object
(input/output/cacheRead/cacheWrite/total) computed by pi/the provider at
request time. We use `usage.cost.total` directly — it's authoritative for
OpenRouter / `:free` / multi-provider models our price table doesn't know.
We only fall back to recomputing via the price table when no numeric total
is present (older sessions / missing field); that fallback charges the full
cacheWrite bucket at the 5m rate, since pi doesn't split 5m/1h.

Validated against a real pi v0.74.0 session (2026-06): header/usage/tree
shapes confirmed. pi also marks tool errors at the message level
(`message.isError`).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Iterable

from watchmen.adapters._shared import (
    extract_skill_from_args,
    extract_skill_from_path,
    normalize_error_signature,
)
from watchmen.metrics import turn_cost_usd

NAME = "pi"

SESSIONS_DIR = Path.home() / ".pi" / "agent" / "sessions"

# Spec version we expect; anything else gets logged + skipped (returns empty result).
SUPPORTED_VERSION = 3


def _parse_iso(ts):
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        # Some examples in the wild use epoch — be lenient.
        try:
            return datetime.fromtimestamp(ts)
        except (OSError, ValueError):
            return None
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def discover() -> Iterable[dict]:
    if not SESSIONS_DIR.exists():
        return
    # Layout: ~/.pi/agent/sessions/--<encoded-cwd>--/<ts>_<uuid>.jsonl
    for jsonl in SESSIONS_DIR.rglob("*.jsonl"):
        yield {
            "path": jsonl,
            "project_dir": None,  # filled from header during scan
            "is_subagent": False,
            "parent_session_id": None,
        }


def _pick_active_leaf(by_id: dict, parent_to_children: dict) -> str | None:
    """Pick the leaf (no children) with the latest timestamp. None if no leaves."""
    leaves = [eid for eid, ent in by_id.items() if eid not in parent_to_children]
    if not leaves:
        return None
    def ts(eid):
        t = _parse_iso(by_id[eid].get("timestamp"))
        return t or datetime.min
    return max(leaves, key=ts)


def _walk_to_root(by_id: dict, leaf_id: str) -> list[dict]:
    """Walk parentId chain from leaf to root, return entries in chronological order."""
    chain: list[dict] = []
    seen = set()
    cur = leaf_id
    while cur and cur in by_id and cur not in seen:
        seen.add(cur)
        chain.append(by_id[cur])
        cur = by_id[cur].get("parentId")
    chain.reverse()
    return chain


def _apply_compaction_cutoff(chain: list[dict]) -> list[dict]:
    """If a compaction entry exists in the walk, drop messages BEFORE its
    firstKeptEntryId. The header (type=session) is always kept."""
    cutoff_id: str | None = None
    for ent in chain:
        if ent.get("type") == "compaction":
            cutoff_id = ent.get("firstKeptEntryId") or cutoff_id  # last one wins
    if not cutoff_id:
        return chain
    # Find the cutoff position and keep [header] + [cutoff and onward].
    kept: list[dict] = []
    in_kept = False
    for ent in chain:
        if ent.get("type") == "session":
            kept.append(ent)
            continue
        if ent.get("id") == cutoff_id:
            in_kept = True
        if in_kept:
            kept.append(ent)
    return kept


def scan(entry: dict):
    path: Path = entry["path"]

    session = {
        "session_id": path.stem,
        "project_dir": None,
        "transcript_path": str(path),
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
        "tool_error_count": 0,
        "models": "[]",
        "input_tokens": 0,
        "cache_creation_tokens": 0,
        "cache_read_tokens": 0,
        "output_tokens": 0,
        "model_dominant": None,
        "cost_usd": 0.0,
        "agent": NAME,
    }
    prompts: list = []
    tool_calls: list = []
    models: set[str] = set()
    model_output_tokens: dict[str, int] = {}
    is_first_user = True
    # Per-skill cost attribution (#94). A skill is detected when the model
    # reads its SKILL.md (a read/bash toolCall whose args contain the path);
    # we accrue subsequent per-message cost into that skill's tool_call row
    # until the next skill read or the next genuine user prompt.
    active_skill: dict | None = None
    # pi emits each tool result as its own `toolResult` message linked to its
    # call by `toolCallId`; index rows so we can stamp is_error/signature back
    # onto the right one. (#110 friction ledger.)
    tool_rows_by_id: dict[str, dict] = {}

    # Pass 1: load all entries.
    by_id: dict[str, dict] = {}
    parent_to_children: dict[str, list[str]] = {}
    header: dict | None = None
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ent = json.loads(line)
            except json.JSONDecodeError:
                continue
            eid = ent.get("id")
            if not eid:
                continue
            by_id[eid] = ent
            if ent.get("type") == "session":
                header = ent
            pid = ent.get("parentId")
            if pid:
                parent_to_children.setdefault(pid, []).append(eid)

    if header:
        version = header.get("version")
        if version not in (SUPPORTED_VERSION, None):
            # Unsupported spec rev — emit nothing rather than misparse.
            return session, prompts, tool_calls
        if header.get("cwd"):
            session["project_dir"] = header["cwd"]
        if header.get("parentSession"):
            session["is_subagent"] = 1
            session["parent_session_id"] = header["parentSession"]

    if not by_id:
        if session["project_dir"] is None:
            session["project_dir"] = "(unknown)"
        return session, prompts, tool_calls

    # Pass 2: pick the active leaf and walk to root.
    leaf = _pick_active_leaf(by_id, parent_to_children)
    if not leaf:
        # No leaves — orphan file. Skip.
        if session["project_dir"] is None:
            session["project_dir"] = "(unknown)"
        return session, prompts, tool_calls

    chain = _walk_to_root(by_id, leaf)
    chain = _apply_compaction_cutoff(chain)

    # Pass 3: extract per-entry stats from the linearized chain.
    for ent in chain:
        ts = ent.get("timestamp")
        if isinstance(ts, str):
            if not session["started_at"] or ts < session["started_at"]:
                session["started_at"] = ts
            if not session["ended_at"] or ts > session["ended_at"]:
                session["ended_at"] = ts

        etype = ent.get("type")
        if etype == "session":
            continue
        if etype == "compaction":
            continue
        if etype != "message":
            # custom / branch_summary / label / etc. — skip.
            continue

        msg = ent.get("message") or {}
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = msg.get("content")
        session["message_count"] += 1

        if role == "user":
            text_parts: list[str] = []
            if isinstance(content, str):
                text_parts.append(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
            text = "\n".join(t for t in text_parts if t)
            if text:
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
                active_skill = None  # genuine prompt ends any skill span

        elif role == "assistant":
            model = msg.get("model")
            if model:
                models.add(model)
            usage = msg.get("usage") or {}
            if isinstance(usage, dict):
                in_t = int(usage.get("input") or 0)
                out_t = int(usage.get("output") or 0)
                cw_t = int(usage.get("cacheWrite") or 0)
                cr_t = int(usage.get("cacheRead") or 0)
                session["input_tokens"] += in_t
                session["cache_creation_tokens"] += cw_t
                session["cache_read_tokens"] += cr_t
                session["output_tokens"] += out_t
                # Prefer pi's own precomputed cost (`usage.cost.total`). It's
                # provider-authoritative — correct for OpenRouter / `:free` /
                # multi-provider models our price table doesn't know. A native
                # total of 0.0 is valid (free models), so only fall back to
                # the price table when no numeric total is present.
                cost_obj = usage.get("cost")
                native = cost_obj.get("total") if isinstance(cost_obj, dict) else None
                if isinstance(native, (int, float)):
                    turn_cost = float(native)
                else:
                    # pi doesn't split cache write into 5m/1h; treat as 5m.
                    turn_cost = turn_cost_usd(model, in_t, cw_t, 0, cr_t, out_t)
                session["cost_usd"] += turn_cost
                if active_skill is not None:
                    active_skill["cost_usd"] = (active_skill["cost_usd"] or 0.0) + turn_cost
                if model:
                    model_output_tokens[model] = model_output_tokens.get(model, 0) + out_t
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "text":
                        session["assistant_text_count"] += 1
                    elif btype == "thinking":
                        session["assistant_thinking_count"] += 1
                    elif btype == "toolCall":
                        session["tool_use_count"] += 1
                        skill_name = extract_skill_from_args(block.get("arguments"))
                        row = {
                            "session_id": session["session_id"],
                            "timestamp": ts,
                            "tool_name": block.get("name") or "?",
                            "is_error": 0,
                            "skill_name": skill_name,
                        }
                        if skill_name:
                            row["cost_usd"] = 0.0
                            active_skill = row
                        block_id = block.get("id")
                        if isinstance(block_id, str) and block_id:
                            tool_rows_by_id[block_id] = row
                        tool_calls.append(row)

        elif role == "toolResult":
            # Tool result lives as its own message. pi carries the error flag
            # at the MESSAGE level (`message.isError`), not inside content
            # blocks — scanning blocks never matched, so tool errors went
            # uncounted. Read the message-level flag (with legacy fallbacks).
            errored = bool(msg.get("isError") or msg.get("is_error"))
            if not errored and isinstance(content, list):
                errored = any(
                    isinstance(b, dict) and (b.get("isError") or b.get("is_error"))
                    for b in content
                )
            if errored:
                session["tool_error_count"] += 1
                # Link back to the call by toolCallId and stamp the signature.
                row = tool_rows_by_id.get(msg.get("toolCallId"))
                if row is not None:
                    row["is_error"] = 1
                    sig = normalize_error_signature(content)
                    if sig:
                        row["error_signature"] = sig

        elif role == "bashExecution":
            session["tool_use_count"] += 1
            # bashExecution carries the command in the message content; treat
            # the whole content blob as a potential SKILL.md reference. Works
            # for both string content ("cat /path/SKILL.md") and list content
            # with text blocks.
            skill_name = extract_skill_from_args(content) or extract_skill_from_path(content if isinstance(content, str) else "")
            row = {
                "session_id": session["session_id"],
                "timestamp": ts,
                "tool_name": "bash",
                "is_error": 0,
                "skill_name": skill_name,
            }
            if skill_name:
                row["cost_usd"] = 0.0
                active_skill = row
            tool_calls.append(row)

        # custom / branchSummary / compactionSummary — silently ignore.

    session["models"] = json.dumps(sorted(models))
    if model_output_tokens:
        session["model_dominant"] = max(model_output_tokens.items(), key=lambda kv: kv[1])[0]
    a = _parse_iso(session["started_at"])
    b = _parse_iso(session["ended_at"])
    if a and b:
        session["duration_seconds"] = (b - a).total_seconds()
    if session["project_dir"] is None:
        session["project_dir"] = "(unknown)"
    return session, prompts, tool_calls
