"""OpenAI Codex CLI session adapter.

Reads ~/.codex/sessions/YYYY/MM/DD/rollout-<ISO>-<uuid>.jsonl. Each line:
    {"timestamp": "<ISO>", "type": "<kind>", "payload": {...}}

Relevant types:
  - session_meta:      header with cwd, model_provider, git info, instructions (AGENTS.md)
  - turn_context:      per-turn approval/sandbox/model state (this is where the model lives)
  - response_item:     model I/O — assistant/user messages, function_call, custom_tool_call,
                       reasoning, web_search_call, tool_search_call
  - event_msg:         CLI internals; token_count carries per-turn deltas in last_token_usage

Quirks handled:
  1. Synthetic injections — first response_items with role=developer and role=user content
     starting with "<environment_context>" / "<INSTRUCTIONS>" / "<permissions" / "<model_switch>"
     are NOT real user turns. Skip them.
  2. user_message duplication — event_msg with type=user_message duplicates response_item;
     we count only response_items so this never gets double-counted.
  3. Cumulative token totals — token_count.info.total_token_usage is cumulative, but
     last_token_usage is per-turn. Use last_token_usage for per-turn cost attribution.
  4. No per-message model — pull from most recent turn_context event.
  5. ~/.codex/history.jsonl is a different schema; we ignore it.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Iterable

from watchmen.adapters._shared import extract_skill_from_args
from watchmen.metrics import price_for_model

NAME = "codex"

SESSIONS_DIR = Path.home() / ".codex" / "sessions"

# Lines we filter from user_prompt_count — synthetic context the CLI injects.
_SYNTHETIC_PREFIXES = (
    "<environment_context>",
    "<INSTRUCTIONS>",
    "<permissions ",
    "<permissions>",
    "<model_switch>",
    "<user_instructions>",
)


def _parse_iso(ts: str | None):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def discover() -> Iterable[dict]:
    if not SESSIONS_DIR.exists():
        return
    # Path layout is YYYY/MM/DD/rollout-*.jsonl — recursive glob is fine.
    for jsonl in SESSIONS_DIR.rglob("rollout-*.jsonl"):
        yield {
            "path": jsonl,
            "project_dir": None,  # filled in from session_meta.cwd during scan
            "is_subagent": False,
            "parent_session_id": None,
        }


def _is_synthetic_user(text: str) -> bool:
    s = text.lstrip()
    return any(s.startswith(prefix) for prefix in _SYNTHETIC_PREFIXES)


def _parse_session_source(value) -> tuple[int, str | None]:
    """Map codex 0.133.0+ ``session_meta.source`` to (is_subagent, parent_session_id).

    Serialization shape (from codex SessionSource / SubAgentSource enums):

    - Main session (Cli / VSCode / Exec / Mcp / Custom / unknown):
      ``"cli"``, ``"vscode"``, ``"exec"``, ``"mcp"``, ``{"custom": "..."}``
      → ``is_subagent=0``.
    - User-facing subagent (spawned via ``spawn_agent``):
      ``{"subagent": {"thread_spawn": {"parent_thread_id": "...", "depth": N,
      "agent_role": "explore", ...}}}`` → ``is_subagent=1`` with parent id.
    - Internal subagents (Review / Compact / MemoryConsolidation):
      ``{"subagent": "review"}`` etc. → ``is_subagent=0``. These are codex's
      own bookkeeping turns, not user-facing delegation, and should not
      inflate subagent cost-share metrics.
    - Pre-0.133.0 rollouts: ``source`` is absent or a plain string we don't
      recognize → falls through to ``is_subagent=0``.

    Unknown shapes return ``(0, None)`` so an unfamiliar future variant
    silently degrades to the pre-0.133.0 default instead of raising.
    """
    if isinstance(value, dict):
        subagent = value.get("subagent")
        if isinstance(subagent, dict):
            spawn = subagent.get("thread_spawn")
            if isinstance(spawn, dict):
                parent = spawn.get("parent_thread_id")
                return 1, str(parent) if parent else None
    return 0, None


def _text_from_content(content) -> str:
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        # OpenAI Responses API: input_text (user) or output_text (assistant)
        if block.get("type") in ("input_text", "output_text", "text"):
            parts.append(block.get("text", ""))
    return "\n".join(parts)


def _codex_turn_cost(model: str | None, last_usage: dict) -> float:
    """Codex usage convention: input_tokens includes cached_input_tokens.
    Anthropic price table has separate cache_read pricing; for OpenAI we approximate
    cached at 0.1× input (matches GPT-5 cache discount). reasoning_output_tokens
    are charged as output."""
    input_total = int(last_usage.get("input_tokens") or 0)
    cached = int(last_usage.get("cached_input_tokens") or 0)
    output = int(last_usage.get("output_tokens") or 0)
    reasoning = int(last_usage.get("reasoning_output_tokens") or 0)
    uncached = max(0, input_total - cached)
    p_in, _p_5m, _p_1h, p_cr, p_out = price_for_model(model)
    return (uncached * p_in + cached * p_cr + (output + reasoning) * p_out) / 1_000_000


def scan(entry: dict):
    path: Path = entry["path"]
    session = {
        "session_id": path.stem,  # rollout-<timestamp>-<uuid>; will be overridden if session_meta.id is present
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
        "cache_creation_tokens": 0,  # OpenAI doesn't surface cache writes; always 0
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
    current_model: str | None = None

    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue

            ts = e.get("timestamp")
            if ts:
                if not session["started_at"] or ts < session["started_at"]:
                    session["started_at"] = ts
                if not session["ended_at"] or ts > session["ended_at"]:
                    session["ended_at"] = ts

            etype = e.get("type")
            payload = e.get("payload") or {}

            if etype == "session_meta":
                # Prefer the canonical session id from the header; cwd → project_dir.
                if payload.get("id"):
                    session["session_id"] = payload["id"]
                if payload.get("cwd"):
                    session["project_dir"] = payload["cwd"]
                # Codex 0.133.0+ exposes session lineage via `source`. For
                # earlier versions this is a plain string we ignore.
                if "source" in payload:
                    is_subagent, parent = _parse_session_source(payload["source"])
                    session["is_subagent"] = is_subagent
                    session["parent_session_id"] = parent
                continue

            if etype == "turn_context":
                m = payload.get("model")
                if m:
                    current_model = m
                    models.add(m)
                continue

            if etype == "event_msg":
                pt = payload.get("type")
                if pt == "token_count":
                    info = payload.get("info") or {}
                    last = info.get("last_token_usage") or {}
                    if last:
                        in_t = int(last.get("input_tokens") or 0)
                        cached = int(last.get("cached_input_tokens") or 0)
                        out_t = int(last.get("output_tokens") or 0)
                        reasoning = int(last.get("reasoning_output_tokens") or 0)
                        session["input_tokens"] += max(0, in_t - cached)
                        session["cache_read_tokens"] += cached
                        session["output_tokens"] += out_t + reasoning
                        session["cost_usd"] += _codex_turn_cost(current_model, last)
                        if current_model:
                            model_output_tokens[current_model] = (
                                model_output_tokens.get(current_model, 0) + out_t + reasoning
                            )
                elif pt == "error":
                    session["tool_error_count"] += 1
                continue

            if etype != "response_item":
                continue

            ptype = payload.get("type")
            role = payload.get("role")
            session["message_count"] += 1

            if ptype == "message":
                text = _text_from_content(payload.get("content"))
                if role == "user":
                    if not text or _is_synthetic_user(text):
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
                elif role == "assistant":
                    if text:
                        session["assistant_text_count"] += 1
                # role=developer → synthetic, skip silently
            elif ptype == "reasoning":
                session["assistant_thinking_count"] += 1
            elif ptype in ("function_call", "custom_tool_call"):
                session["tool_use_count"] += 1
                # Codex serializes tool args as a JSON-encoded string. Parse
                # best-effort; if it doesn't decode (corrupt line, non-JSON
                # custom tool, etc.) we still fall through to the raw string
                # match, which catches paths embedded in shell commands.
                raw_args = payload.get("arguments")
                parsed_args = raw_args
                if isinstance(raw_args, str):
                    try:
                        parsed_args = json.loads(raw_args)
                    except json.JSONDecodeError:
                        parsed_args = raw_args
                tool_calls.append({
                    "session_id": session["session_id"],
                    "timestamp": ts,
                    "tool_name": payload.get("name") or "?",
                    "is_error": 0,
                    "skill_name": extract_skill_from_args(parsed_args),
                })
            elif ptype in ("function_call_output", "custom_tool_call_output"):
                # Tool result. is_error not directly exposed; some outputs carry
                # exit_code in their JSON. Best-effort scan.
                out = payload.get("output")
                if isinstance(out, str) and ('"exit_code":1' in out or '"exit_code": 1' in out):
                    session["tool_error_count"] += 1
            elif ptype in ("web_search_call", "tool_search_call"):
                session["tool_use_count"] += 1
                tool_calls.append({
                    "session_id": session["session_id"],
                    "timestamp": ts,
                    "tool_name": ptype,
                    "is_error": 0,
                    "skill_name": None,
                })

    session["models"] = json.dumps(sorted(models))
    if model_output_tokens:
        session["model_dominant"] = max(model_output_tokens.items(), key=lambda kv: kv[1])[0]
    a = _parse_iso(session["started_at"])
    b = _parse_iso(session["ended_at"])
    if a and b:
        session["duration_seconds"] = (b - a).total_seconds()
    # If we never saw a session_meta, fall back to filename stem (already set).
    if session["project_dir"] is None:
        session["project_dir"] = "(unknown)"
    return session, prompts, tool_calls
