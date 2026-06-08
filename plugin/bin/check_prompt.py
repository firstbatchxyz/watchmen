#!/usr/bin/env python3
"""UserPromptSubmit hook handler.

Reads the prompt from the hook event stdin, FTS5-matches it against the indexed
`when_to_use` triggers for the project resolved from CWD, and writes a
suggestion file at ~/.watchmen/state/<project>.suggestion.json that the
statusLine reads on its next refresh (after the assistant responds).

If no match passes the threshold, any prior suggestion for this project is
cleared — each prompt either has a current suggestion or none.

Never writes to stdout. The hook wrapper redirects all output so nothing
leaks into the agent's context (this is the "inform, don't manipulate" rule).
"""

import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

WATCHMEN = Path.home() / ".watchmen"
INDEX_DB = WATCHMEN / "skill_index.db"
PROJECTS_INDEX = WATCHMEN / "projects.json"
STATE_DIR = WATCHMEN / "state"
SUGGESTIONS_LOG = WATCHMEN / "suggestions.jsonl"


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


# BM25 returns negative numbers; more negative = more relevant. -0.5 keeps the
# bar fairly high. Tunable without a code change as we calibrate against real
# false positives (a stricter bar is just a more-negative number).
SCORE_THRESHOLD = _env_float("WATCHMEN_SUGGEST_THRESHOLD", -0.5)

# Anti-firehose: the hook fires on every prompt, so without a memory the same
# skill gets re-suggested dozens of times per session (observed: one skill
# suggested 75x in a single session). We suggest a given skill at most once per
# session, and not again across sessions until a cooldown elapses. Both knobs
# are env-tunable; 0 disables that layer. See _recently_suggested().
SUGGEST_COOLDOWN_SECONDS = _env_int("WATCHMEN_SUGGEST_COOLDOWN_SECONDS", 6 * 3600)
# Bound the seen-state file: drop records older than this on each write.
_SEEN_TTL_SECONDS = 14 * 24 * 3600

STOP_WORDS = {
    "the", "and", "for", "with", "that", "this", "from", "what", "when", "where",
    "how", "can", "should", "would", "could", "you", "your", "please", "help",
    "need", "want", "make", "just", "any", "all", "let", "into", "onto", "also",
    "but", "not", "get", "got", "have", "has", "are", "was", "were", "will",
    "did", "does", "doing", "done", "use", "using", "used", "try", "tried",
    "give", "gave", "take", "took", "set", "let", "now", "then", "than",
}


def resolve_project_key(cwd: str) -> str | None:
    if not PROJECTS_INDEX.exists() or not cwd:
        return None
    try:
        projects = json.loads(PROJECTS_INDEX.read_text())
        cwd_path = Path(cwd).resolve()
    except (json.JSONDecodeError, OSError):
        return None
    if not cwd_path.exists():
        return None
    best: tuple[int, str] | None = None
    for p in projects:
        repo = p.get("source_repo")
        key = p.get("project_key")
        if not (repo and key):
            continue
        try:
            repo_abs = Path(repo).resolve()
            if not repo_abs.exists():
                continue
        except OSError:
            continue
        for c in [cwd_path, *cwd_path.parents]:
            try:
                if c.samefile(repo_abs):
                    if best is None or len(str(repo_abs)) > best[0]:
                        best = (len(str(repo_abs)), key)
                    break
            except OSError:
                continue
    return best[1] if best else None


def sanitize_fts_query(text: str) -> str:
    """Reduce a prompt to keyword tokens safe for FTS5 MATCH. Returns OR-joined
    tokens — let BM25 rank by how many fire."""
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9]{2,}", text or "")
    keep = [t.lower() for t in tokens if t.lower() not in STOP_WORDS]
    if not keep:
        return ""
    # FTS5 escape: quote each token to disable operator parsing.
    return " OR ".join(f'"{t}"' for t in keep[:20])


def write_suggestion(project_key: str, suggestion: dict | None) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    target = STATE_DIR / f"{project_key}.suggestion.json"
    if suggestion is None:
        target.unlink(missing_ok=True)
        return
    target.write_text(json.dumps(suggestion, indent=2))


def _seen_path(project_key: str) -> Path:
    return STATE_DIR / f"{project_key}.suggest_seen.json"


def _load_seen(project_key: str) -> dict:
    """Per-(session, skill) record of when we last surfaced a suggestion.

    Shape: {"<session_id>|<skill_slug>": <unix_ts>}. Used to suppress repeats
    so a skill is surfaced once per session and not again until the cooldown
    elapses. Corrupt/missing state degrades to "nothing seen" — at worst we
    show one extra suggestion, never a crash in the hot path of every prompt.
    """
    try:
        data = json.loads(_seen_path(project_key).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _recently_suggested(seen: dict, session_id: str | None, skill_slug: str, now: float) -> bool:
    """True if this skill should be suppressed for this prompt.

    Same session: suppress any repeat (suggest once per session). Across
    sessions: suppress until SUGGEST_COOLDOWN_SECONDS has elapsed since the
    last time we surfaced it anywhere. A cooldown of 0 disables the
    cross-session layer (same-session dedup still applies).
    """
    # Same-session "suggest once" only applies when we actually have a session
    # identity. With no session_id every prompt would share the "?" bucket, so
    # a bare presence check would mute the skill forever (cooldown never
    # consulted, the suppressed path never re-stamps or prunes it). No session
    # → fall through to the time-based cooldown, which is the right bound there.
    if session_id and f"{session_id}|{skill_slug}" in seen:
        return True
    if SUGGEST_COOLDOWN_SECONDS <= 0:
        return False
    # Only numeric stamps count: a corrupt or hand-edited state file could hold
    # a non-number, and `now - last` on a string would raise in the hot path of
    # every prompt. Ignoring it falls back to "not recently suggested" — at
    # worst we show one extra suggestion, and _record_seen then rewrites a clean
    # numeric value (and prunes the junk).
    last = max(
        (ts for key, ts in seen.items()
         if key.split("|", 1)[-1] == skill_slug and isinstance(ts, (int, float))),
        default=None,
    )
    return last is not None and (now - last) < SUGGEST_COOLDOWN_SECONDS


def _record_seen(project_key: str, seen: dict, session_id: str | None, skill_slug: str, now: float) -> None:
    """Stamp this suggestion and prune expired records, then persist."""
    seen[f"{session_id or '?'}|{skill_slug}"] = now
    fresh = {k: ts for k, ts in seen.items() if isinstance(ts, (int, float)) and now - ts < _SEEN_TTL_SECONDS}
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        _seen_path(project_key).write_text(json.dumps(fresh))
    except OSError:
        pass


def main() -> int:
    raw = sys.stdin.read()
    if not raw:
        return 0
    try:
        evt = json.loads(raw)
    except json.JSONDecodeError:
        return 0
    if evt.get("hook_event_name") != "UserPromptSubmit":
        return 0

    prompt = (evt.get("prompt") or "").strip()
    cwd = evt.get("cwd") or os.getcwd()
    project_key = resolve_project_key(cwd)
    if not project_key:
        return 0

    if not INDEX_DB.exists():
        return 0

    query = sanitize_fts_query(prompt)
    if not query:
        write_suggestion(project_key, None)
        return 0

    try:
        with sqlite3.connect(str(INDEX_DB)) as conn:
            row = conn.execute(
                "SELECT skill_slug, bm25(skill_match) AS score "
                "FROM skill_match "
                "WHERE skill_match MATCH ? AND project_key = ? "
                "ORDER BY score LIMIT 1",
                (query, project_key),
            ).fetchone()
    except sqlite3.Error:
        return 0

    if not row:
        write_suggestion(project_key, None)
        return 0

    skill_slug, score = row
    if score is None or score > SCORE_THRESHOLD:
        write_suggestion(project_key, None)
        return 0

    # Anti-firehose: if we've already surfaced this skill (this session, or
    # within the cooldown), stay silent rather than re-asserting it every
    # prompt. Leave any prior suggestion file untouched — the user has already
    # been informed once; nagging is how a useful signal becomes noise.
    session_id = evt.get("session_id")
    now = time.time()
    # Dedup must never break prompt submission: any unexpected failure here
    # fails open (treat as not-recently-suggested) rather than raising.
    seen: dict = {}
    try:
        seen = _load_seen(project_key)
        if _recently_suggested(seen, session_id, skill_slug, now):
            return 0
    except Exception:
        seen = {}

    suggestion = {
        "schema": 1,
        "ts": time.strftime("%Y-%m-%dT%H:%M"),
        "skill_slug": skill_slug,
        "score": round(score, 3),
        "prompt_excerpt": prompt[:140] + ("…" if len(prompt) > 140 else ""),
    }
    write_suggestion(project_key, suggestion)
    _record_seen(project_key, seen, session_id, skill_slug, now)

    # Append-only audit log for the metrics aggregator. Each suggestion fire is
    # one JSON line; the aggregator joins with subsequent prompts to compute
    # uptake. Include session_id so we can correlate within a session window.
    try:
        SUGGESTIONS_LOG.parent.mkdir(parents=True, exist_ok=True)
        with SUGGESTIONS_LOG.open("a") as fh:
            fh.write(json.dumps({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "project_key": project_key,
                "session_id": evt.get("session_id"),
                "skill_slug": skill_slug,
                "score": round(score, 3),
                "prompt_excerpt": suggestion["prompt_excerpt"],
            }) + "\n")
    except OSError:
        pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
