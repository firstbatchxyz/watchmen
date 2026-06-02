"""Claude Code session adapter.

Reads ~/.claude/projects/<encoded-cwd>/<session-id>.jsonl plus subagent
transcripts under <encoded-cwd>/<parent-session>/subagents/.

Schema notes from Claude Code transcripts:
  Each line: {timestamp, type: "user"|"assistant", message: {content, model?, usage?}}
  - user.content can be a string OR a list with text/tool_result blocks
  - assistant.content is a list with text/thinking/tool_use blocks
  - usage carries cache_creation.ephemeral_5m_input_tokens / _1h variants
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Iterable

from watchmen.adapters._shared import normalize_error_signature
from watchmen.metrics import turn_cost_usd
from watchmen.paths import decode_project_dir

NAME = "claude_code"

PROJECTS_DIR = Path.home() / ".claude" / "projects"

# Cache: encoded dir name → decoded real cwd. Filesystem walk is cheap but we
# do it once per encoded dir, not once per transcript.
_DECODE_CACHE: dict[str, str] = {}


def _parse_iso(ts: str | None):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _resolve(encoded: str) -> str:
    if encoded not in _DECODE_CACHE:
        _DECODE_CACHE[encoded] = decode_project_dir(encoded)
    return _DECODE_CACHE[encoded]


def discover() -> Iterable[dict]:
    if not PROJECTS_DIR.exists():
        return
    for project_dir in PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        decoded = _resolve(project_dir.name)
        for jsonl in project_dir.glob("*.jsonl"):
            yield {
                "path": jsonl,
                "project_dir": decoded,
                "is_subagent": False,
                "parent_session_id": None,
            }
        for sub_dir in project_dir.iterdir():
            if not sub_dir.is_dir():
                continue
            sub_path = sub_dir / "subagents"
            if not sub_path.exists():
                continue
            for sub_jsonl in sub_path.glob("*.jsonl"):
                yield {
                    "path": sub_jsonl,
                    "project_dir": decoded,
                    "is_subagent": True,
                    "parent_session_id": sub_dir.name,
                }


def scan(entry: dict):
    path: Path = entry["path"]
    project_dir = entry["project_dir"]
    is_subagent = entry["is_subagent"]
    parent_sid = entry["parent_session_id"]

    session = {
        "session_id": path.stem,
        "project_dir": project_dir,
        "transcript_path": str(path),
        "started_at": None,
        "ended_at": None,
        "duration_seconds": None,
        "is_subagent": int(is_subagent),
        "parent_session_id": parent_sid,
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
    is_first = True
    # Claude Code splits one logical assistant response (thinking + text +
    # tool_use blocks) across multiple JSONL lines that SHARE one
    # `message.id` and REPEAT the same `usage`. Summing usage per line
    # overcounts tokens/cost ~1.5-3.3x. We charge usage once per contiguous
    # run of same-id assistant lines (runs are always contiguous in practice)
    # while still counting content blocks per line below.
    last_usage_msg_id: str | None = None
    # Per-skill cost attribution (#94). A `Skill` tool_use starts a span; we
    # accrue each subsequent turn's cost into that skill's tool_call row until
    # the next Skill or the next genuine user prompt. `active_skill` points at
    # the row we're accruing into. The activating turn itself isn't charged
    # (its cost is trivial; the SKILL.md context + work land on later turns).
    active_skill: dict | None = None
    # A tool_use's error arrives later, in a separate `tool_result` block on a
    # following `user` message, linked by id. Index rows by their tool_use id
    # so we can stamp `is_error` + `error_signature` back onto the right row
    # when its result lands. (#110 friction ledger.)
    tool_rows_by_id: dict[str, dict] = {}

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
            if etype not in ("user", "assistant"):
                continue

            session["message_count"] += 1
            msg = e.get("message", {}) or {}
            content = msg.get("content")

            if etype == "user":
                if e.get("isMeta"):
                    # Injected context (e.g. the SKILL.md body Claude Code
                    # feeds in after a Skill call) — not a real user prompt,
                    # so don't count it or end the active skill span.
                    continue
                if isinstance(content, str):
                    text = content
                    prompts.append({
                        "session_id": session["session_id"],
                        "timestamp": ts,
                        "text": text,
                        "word_count": len(text.split()),
                        "char_count": len(text),
                        "is_first_in_session": int(is_first),
                    })
                    is_first = False
                    session["user_prompt_count"] += 1
                    active_skill = None  # genuine prompt ends any skill span
                elif isinstance(content, list):
                    text_parts: list[str] = []
                    saw_tool_result = False
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        btype = block.get("type")
                        if btype == "text":
                            text_parts.append(block.get("text", ""))
                        elif btype == "tool_result":
                            saw_tool_result = True
                            if block.get("is_error"):
                                session["tool_error_count"] += 1
                                # Stamp the failure back onto the originating
                                # tool_use row. `is_error` mirrors the session
                                # error count (includes user cancels), but the
                                # `error_signature` is only set for genuine
                                # agent friction (the normalizer returns None
                                # for cancel/reject), so the ledger stays clean.
                                row = tool_rows_by_id.get(block.get("tool_use_id"))
                                if row is not None:
                                    row["is_error"] = 1
                                    sig = normalize_error_signature(block.get("content"))
                                    if sig:
                                        row["error_signature"] = sig
                    if text_parts and not saw_tool_result:
                        text = "\n".join(text_parts)
                        prompts.append({
                            "session_id": session["session_id"],
                            "timestamp": ts,
                            "text": text,
                            "word_count": len(text.split()),
                            "char_count": len(text),
                            "is_first_in_session": int(is_first),
                        })
                        is_first = False
                        session["user_prompt_count"] += 1
                        active_skill = None  # genuine prompt ends any skill span

            elif etype == "assistant":
                model = msg.get("model")
                if model:
                    models.add(model)
                usage = msg.get("usage") or {}
                msg_id = msg.get("id")
                # Skip usage for continuation lines of the same logical
                # message (same id repeats identical usage). Block-type
                # tallies below still run per line.
                charge_usage = msg_id is None or msg_id != last_usage_msg_id
                last_usage_msg_id = msg_id
                if isinstance(usage, dict) and charge_usage:
                    in_t = int(usage.get("input_tokens") or 0)
                    cc_t = int(usage.get("cache_creation_input_tokens") or 0)
                    cr_t = int(usage.get("cache_read_input_tokens") or 0)
                    out_t = int(usage.get("output_tokens") or 0)
                    cache_creation = usage.get("cache_creation") or {}
                    cc_5m = int(cache_creation.get("ephemeral_5m_input_tokens") or 0) if isinstance(cache_creation, dict) else 0
                    cc_1h = int(cache_creation.get("ephemeral_1h_input_tokens") or 0) if isinstance(cache_creation, dict) else 0
                    if cc_5m + cc_1h == 0 and cc_t > 0:
                        cc_5m = cc_t  # default-bucket fallback
                    session["input_tokens"] += in_t
                    session["cache_creation_tokens"] += cc_t
                    session["cache_read_tokens"] += cr_t
                    session["output_tokens"] += out_t
                    turn_cost = turn_cost_usd(model, in_t, cc_5m, cc_1h, cr_t, out_t)
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
                        elif btype == "tool_use":
                            session["tool_use_count"] += 1
                            tool_name = block.get("name", "?")
                            # Claude Code records skill activations as a
                            # `Skill` tool_use with `input.skill = '<slug>'`.
                            # Capture the slug separately so prune can count
                            # per-skill usage without re-parsing transcripts.
                            skill_name: str | None = None
                            if tool_name == "Skill":
                                inp = block.get("input") or {}
                                if isinstance(inp, dict):
                                    slug = inp.get("skill")
                                    if isinstance(slug, str) and slug.strip():
                                        skill_name = slug.strip()
                            row = {
                                "session_id": session["session_id"],
                                "timestamp": ts,
                                "tool_name": tool_name,
                                "is_error": 0,
                                "skill_name": skill_name,
                            }
                            if skill_name:
                                # Start a fresh attribution span; later turns
                                # accrue their cost into this row.
                                row["cost_usd"] = 0.0
                                active_skill = row
                            block_id = block.get("id")
                            if isinstance(block_id, str) and block_id:
                                tool_rows_by_id[block_id] = row
                            tool_calls.append(row)

    session["models"] = json.dumps(sorted(models))
    if model_output_tokens:
        session["model_dominant"] = max(model_output_tokens.items(), key=lambda kv: kv[1])[0]
    a = _parse_iso(session["started_at"])
    b = _parse_iso(session["ended_at"])
    if a and b:
        session["duration_seconds"] = (b - a).total_seconds()
    return session, prompts, tool_calls
