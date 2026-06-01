"""Longitudinal analyst over Claude Code sessions.

Walks days in chronological order. Each day, an LLM agent reads the prior running
thesis + that day's session activity, drills into interesting sessions via tools,
and produces an updated thesis. Output is written to analyses/<project>/<date>.md
plus _running.md (the latest thesis).

Usage:
  uv run analyze.py -p tally-weijl-images --limit-days 1     # pilot one day
  uv run analyze.py -p tally-weijl-images                    # full repo
  uv run analyze.py --model openrouter-slug                  # swap model
"""

import argparse
import json
import sqlite3
import time
from pathlib import Path
from textwrap import dedent

import httpx

from watchmen.agent import chat_call
from watchmen.corpus_filters import substantive_filter
from watchmen.paths import ANALYSES_DIR, CORPUS_DB

ROOT = Path(__file__).parent


def _default_model() -> str:
    """Resolve the analyst's default model via the active provider, lazily
    so a user who switches provider after import sees the new default."""
    from watchmen import config
    return config.default_model()


# Module-level alias for external imports that referenced `DEFAULT_MODEL`
# directly. Resolved at import time against the current active provider.
DEFAULT_MODEL = _default_model()


# ─── Tools ──────────────────────────────────────────────────────────────────

def _tracked_source_repo(project_key: str | None) -> str | None:
    if not project_key:
        return None
    try:
        from watchmen import state
        state.init_db()
        proj = state.get_project(project_key)
        return proj.get("source_repo") if proj else None
    except Exception:
        return None


def _project_dir_predicate(source_repo: str, alias: str = "s") -> tuple[str, tuple[str, str]]:
    root = str(Path(source_repo).expanduser())
    return f"({alias}.project_dir = ? OR {alias}.project_dir LIKE ?)", (root, root.rstrip("/") + "/%")


def tool_list_activity_on(date: str, project_substr: str | None = None):
    conn = sqlite3.connect(CORPUS_DB)
    conn.row_factory = sqlite3.Row
    where = ["substr(p.timestamp, 1, 10) = ?", "s.is_subagent = 0", substantive_filter("s")]
    params: list = [date]
    if project_substr:
        source_repo = _tracked_source_repo(project_substr)
        if source_repo:
            pred, pred_params = _project_dir_predicate(source_repo)
            where.append(pred)
            params.extend(pred_params)
        else:
            where.append("s.project_dir = ?")
            params.append(project_substr)
    rows = conn.execute(
        f"""
        SELECT s.session_id, s.project_dir,
               substr(s.started_at, 1, 10) AS session_start_day,
               s.user_prompt_count AS session_total_prompts,
               s.tool_use_count AS session_total_tools,
               s.tool_error_count AS session_total_errors,
               COUNT(*) AS prompts_today
        FROM prompts p JOIN sessions s ON p.session_id = s.session_id
        WHERE {' AND '.join(where)}
        GROUP BY s.session_id
        ORDER BY prompts_today DESC
        """,
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def tool_read_session_prompts(session_id: str, day: str | None = None, max_chars: int = 30000):
    conn = sqlite3.connect(CORPUS_DB)
    conn.row_factory = sqlite3.Row
    where = ["session_id = ?"]
    params: list = [session_id]
    if day:
        where.append("substr(timestamp, 1, 10) = ?")
        params.append(day)
    rows = conn.execute(
        f"SELECT timestamp, text FROM prompts WHERE {' AND '.join(where)} ORDER BY id",
        params,
    ).fetchall()
    parts: list[str] = []
    total = 0
    for i, r in enumerate(rows):
        if total > max_chars:
            parts.append(f"[... {len(rows) - i} more prompts truncated ...]")
            break
        snippet = (r["text"] or "")[:1500]
        parts.append(f"[{(r['timestamp'] or '?')[:19]}] {snippet}")
        total += len(snippet)
    return "\n\n---\n\n".join(parts) or "(no prompts)"


def tool_read_session_full(session_id: str, max_chars: int = 30000):
    conn = sqlite3.connect(CORPUS_DB)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT transcript_path FROM sessions WHERE session_id = ?", (session_id,)
    ).fetchone()
    if not row or not row["transcript_path"]:
        return f"session not found: {session_id}"
    parts: list[str] = []
    total = 0
    with open(row["transcript_path"], encoding="utf-8") as f:
        for line in f:
            if total > max_chars:
                parts.append("[... truncated ...]")
                break
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = e.get("type")
            if etype not in ("user", "assistant"):
                continue
            ts = (e.get("timestamp") or "?")[:19]
            msg = e.get("message", {}) or {}
            content = msg.get("content")
            if isinstance(content, str):
                snippet = content[:600]
                line_str = f"[{ts}] user: {snippet}"
                parts.append(line_str)
                total += len(line_str)
            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "text":
                        prefix = "user" if etype == "user" else "assistant"
                        snippet = (block.get("text") or "")[:600]
                        line_str = f"[{ts}] {prefix}: {snippet}"
                    elif btype == "tool_use":
                        name = block.get("name", "?")
                        inp = block.get("input", {})
                        keys = ", ".join(list(inp.keys())[:3]) if isinstance(inp, dict) else ""
                        line_str = f"[{ts}] tool: {name}({keys})"
                    elif btype == "tool_result":
                        is_err = bool(block.get("is_error"))
                        c = block.get("content", "")
                        if isinstance(c, list):
                            c = " ".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in c)
                        snippet = str(c)[:200]
                        prefix = "tool_error" if is_err else "tool_result"
                        line_str = f"[{ts}] {prefix}: {snippet}"
                    else:
                        continue
                    parts.append(line_str)
                    total += len(line_str)
    return "\n".join(parts) or "(empty)"


def tool_query_corpus(sql: str, max_rows: int = 50):
    if not sql.strip().lower().startswith("select"):
        return "ERROR: only SELECT statements allowed"
    conn = sqlite3.connect(CORPUS_DB)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(sql).fetchmany(max_rows)
    except sqlite3.Error as e:
        return f"ERROR: {e}"
    return json.dumps([dict(r) for r in rows], default=str, indent=2)


def tool_fetch_pr_status(repo: str, pr_number: int, host: str | None = None):
    """Look up a pull request's review state and latest comments.

    Args:
      repo: 'owner/name' (e.g. 'astral-sh/uv') or a full URL.
      pr_number: integer PR number.
      host: optional. If given, e.g. 'kai-bench-forgejo-production.up.railway.app',
            uses that Forgejo/Gitea host. Otherwise github.com.

    Returns JSON with: state, merged, reviews (list of {state, user, body}),
    comments (latest 5 review-comments), and a short summary.

    Auth: GITHUB_TOKEN for github.com; WATCHMEN_FORGEJO_TOKEN for forgejo hosts.
    """
    import os
    import urllib.parse

    repo = repo.strip()
    if repo.startswith("http"):
        u = urllib.parse.urlparse(repo)
        host = host or u.netloc
        path = u.path.strip("/").split("/")
        if len(path) >= 2:
            owner, name = path[0], path[1]
        else:
            return f"ERROR: bad repo url: {repo}"
    elif "/" in repo:
        owner, name = repo.split("/", 1)
    else:
        return f"ERROR: bad repo spec: {repo!r}; expected 'owner/name' or URL"

    if host is None or "github.com" in host:
        api_base = "https://api.github.com"
        token = os.environ.get("GITHUB_TOKEN", "")
    else:
        scheme = "https" if not host.startswith("http") else ""
        host_clean = host.replace("https://", "").replace("http://", "").rstrip("/")
        api_base = f"{scheme}://{host_clean}/api/v1" if scheme else f"https://{host_clean}/api/v1"
        token = os.environ.get("WATCHMEN_FORGEJO_TOKEN", "") or os.environ.get("FORGEJO_API_TOKEN", "")

    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = (
            f"token {token}" if "github.com" not in api_base else f"Bearer {token}"
        )

    try:
        with httpx.Client(timeout=20.0, headers=headers) as client:
            pr_r = client.get(f"{api_base}/repos/{owner}/{name}/pulls/{pr_number}")
            if pr_r.status_code != 200:
                return f"ERROR: PR fetch {pr_r.status_code}: {pr_r.text[:200]}"
            pr = pr_r.json()
            rev_r = client.get(f"{api_base}/repos/{owner}/{name}/pulls/{pr_number}/reviews")
            reviews = rev_r.json() if rev_r.status_code == 200 else []
            com_r = client.get(f"{api_base}/repos/{owner}/{name}/issues/{pr_number}/comments")
            comments = com_r.json() if com_r.status_code == 200 else []
    except Exception as e:
        return f"ERROR: HTTP failure: {e}"

    reviews_terse = [
        {
            "state": (r.get("state") or "").upper(),
            "user": (r.get("user") or {}).get("login"),
            "body": (r.get("body") or "")[:600],
            "submitted_at": r.get("submitted_at"),
        }
        for r in (reviews or [])
        if isinstance(r, dict)
    ]
    comments_terse = [
        {
            "user": (c.get("user") or {}).get("login"),
            "body": (c.get("body") or "")[:600],
            "created_at": c.get("created_at"),
        }
        for c in (comments or [])[-5:]
        if isinstance(c, dict)
    ]

    out = {
        "owner_repo": f"{owner}/{name}",
        "number": pr_number,
        "state": pr.get("state"),
        "merged": pr.get("merged"),
        "title": pr.get("title"),
        "review_count": len(reviews_terse),
        "reviews": reviews_terse[-5:],
        "recent_comments": comments_terse,
    }
    return json.dumps(out, default=str, indent=2)


TOOLS = [
    {"type": "function", "function": {
        "name": "list_activity_on",
        "description": "List sessions with prompt activity on a specific date. Optionally scope by project_dir substring.",
        "parameters": {"type": "object", "properties": {
            "date": {"type": "string", "description": "YYYY-MM-DD"},
            "project_substr": {"type": "string", "description": "Optional substring of project_dir to scope to (e.g. 'tally-weijl-images')"},
        }, "required": ["date"]},
    }},
    {"type": "function", "function": {
        "name": "read_session_prompts",
        "description": "Return the user prompts of a session (no agent output). Optionally filter to a single day. Truncated to ~30k chars.",
        "parameters": {"type": "object", "properties": {
            "session_id": {"type": "string"},
            "day": {"type": "string", "description": "Optional YYYY-MM-DD filter"},
        }, "required": ["session_id"]},
    }},
    {"type": "function", "function": {
        "name": "read_session_full",
        "description": "Return the rendered conversation (user prompts + assistant text + tool calls + results). Use for deep dives — when prompts alone aren't enough. Truncated to ~30k chars.",
        "parameters": {"type": "object", "properties": {
            "session_id": {"type": "string"},
        }, "required": ["session_id"]},
    }},
    {"type": "function", "function": {
        "name": "query_corpus",
        "description": ("Run a SELECT-only SQL query against corpus.db. Tables: "
                        "sessions(session_id, project_dir, started_at, ended_at, is_subagent, "
                        "message_count, user_prompt_count, tool_use_count, tool_error_count, "
                        "models, duration_seconds); "
                        "prompts(id, session_id, timestamp, text, word_count, char_count, is_first_in_session); "
                        "tool_calls(id, session_id, timestamp, tool_name, is_error)."),
        "parameters": {"type": "object", "properties": {
            "sql": {"type": "string"},
        }, "required": ["sql"]},
    }},
    {"type": "function", "function": {
        "name": "fetch_pr_status",
        "description": (
            "Look up a pull request's review state and latest comments. Use when a session opened "
            "or updated a PR (visible in agent tool calls like create_pull_request, gh pr create, "
            "or kai_create_pull_request, or referenced in prompts). Returns reviews (APPROVED / "
            "CHANGES_REQUESTED / COMMENTED) and the latest comments — these are the maintainer's "
            "feedback on the agent's work."
        ),
        "parameters": {"type": "object", "properties": {
            "repo": {"type": "string", "description": "'owner/name' (e.g. 'astral-sh/uv') or a full URL to the PR/repo."},
            "pr_number": {"type": "integer"},
            "host": {"type": "string", "description": "Optional non-github host (e.g. 'kai-bench-forgejo-production-d845.up.railway.app'). Defaults to github.com."},
        }, "required": ["repo", "pr_number"]},
    }},
    {"type": "function", "function": {
        "name": "update_analysis",
        "description": "FINAL CALL — submit the updated running thesis as markdown. After this call the day's loop ends.",
        "parameters": {"type": "object", "properties": {
            "markdown": {"type": "string"},
        }, "required": ["markdown"]},
    }},
]


# ─── Agent loop ──────────────────────────────────────────────────────────────

SYSTEM_PROMPT = dedent("""
    You are a longitudinal analyst studying how Batuhan uses Claude Code (a coding-agent CLI).
    Your goal: build a running thesis on HOW he interacts with the agent — communication style,
    workflow archetypes, frustration patterns, recurring task shapes, evolving habits.

    You receive ONE day at a time, in chronological order, with the prior day's running thesis.
    Update the thesis with what's new — refine what you already had, don't restart from scratch.

    Be selective. Don't read every session in detail. Drill into the ones that look unusual,
    frustrated, or repetitive. Skim the rest. If a day reveals nothing new, say so concisely.

    When a session opens or updates a pull request, call fetch_pr_status to see how the maintainer
    received it (approved, requested changes, merged, closed) and read any review comments. PR
    review feedback is the highest-signal source of what the agent did NOT know and SHOULD have
    known about THIS codebase. Use it to populate the "Skill candidates" section with concrete,
    codebase-specific entries that would have closed the gap.

    Output structure (markdown):
      # Usage Profile — {project} (running thesis)

      ## Communication style
      ## Workflow archetypes
      ## Frustration / pushback patterns
      ## Skill candidates
      ## Drift / evolution (how usage has changed over time)
      ## Notable sessions

    Section scoping rules:
      - Communication style and Workflow archetypes describe HOW the user works inside this
        project_key. They CAN reference cross-topic content that surfaced in long-running sessions
        tagged to this project (e.g. side-conversations about other repos or personal projects).
      - Skill candidates must be packageable, reusable artifacts that would live in THIS repo's
        skills/ directory. Reject candidates that describe other repos, personal projects
        (investments, stock trading, customer projects rooted in other directories), or non-coding
        workflows — even if they appeared in sessions tagged to this project. The test: would this
        skill actually belong checked into THIS repo? If no, don't list it.

    Tone: factual and concrete. Avoid all-caps emphasis, hype framing, marketing-style phrases
    ("THE PIPELINE GOES LIVE", "STRONGEST POSITIVE SIGNAL"), and dramatic day-counters ("Day 23 of
    mega-session"). State observations plainly: "May 9: curator pipeline shipped to two repos" is
    better than "May 9: THE CURATOR PIPELINE GOES LIVE." If a finding matters, the facts will
    carry it.

    Be concrete. Cite session_ids and dates. When done, call update_analysis with the full updated
    thesis.
""").strip()


def run_day(
    client: httpx.Client,
    headers: dict,
    model: str,
    day: str,
    project_substr: str | None,
    prior_md: str,
    max_iter: int = 24,
    *,
    provider: str | None = None,
):
    """Run one day of analyst.

    `headers` is retained as a positional argument for backward compatibility
    with external callers, but is now unused — the provider abstraction in
    `chat_call` builds the right headers from the active provider at call
    time. New callers can pass `headers={}`.
    """
    user_msg = dedent(f"""
        Today: {day}
        Project scope: {project_substr or '(all projects)'}

        Prior running thesis:
        ---
        {prior_md or '(empty — first day)'}
        ---

        Use list_activity_on('{day}', '{project_substr or ''}') to see what happened today,
        then drill into sessions with read_session_prompts or read_session_full as needed.
        Use query_corpus for cross-session questions.

        When done, call update_analysis with the full updated thesis as markdown.
    """).strip()

    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_msg},
    ]

    final_md: str | None = None
    for _ in range(max_iter):
        data = chat_call(
            client,
            messages,
            model=model,
            tools=TOOLS,
            provider=provider,
            agent_name="analyst",
        )
        msg = data["choices"][0]["message"]

        clean = {"role": "assistant", "content": msg.get("content") or ""}
        if msg.get("tool_calls"):
            clean["tool_calls"] = msg["tool_calls"]
        messages.append(clean)

        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            final_md = msg.get("content") or None
            break

        ended = False
        for tc in tool_calls:
            fn = tc["function"]["name"]
            try:
                args = json.loads(tc["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}

            if fn == "update_analysis":
                final_md = args.get("markdown") or ""
                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": "saved"})
                ended = True
                continue
            try:
                if fn == "list_activity_on":
                    result = json.dumps(tool_list_activity_on(**args), default=str)
                elif fn == "read_session_prompts":
                    result = tool_read_session_prompts(**args)
                elif fn == "read_session_full":
                    result = tool_read_session_full(**args)
                elif fn == "query_corpus":
                    result = tool_query_corpus(**args)
                elif fn == "fetch_pr_status":
                    result = tool_fetch_pr_status(**args)
                else:
                    result = f"unknown tool: {fn}"
            except Exception as e:
                result = f"ERROR: {e}"
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result[:30000]})

        if ended:
            break

    return final_md


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", "-p", help="project_dir substring filter (e.g. 'tally-weijl-images')")
    parser.add_argument("--model", "-m", default=DEFAULT_MODEL)
    parser.add_argument("--limit-days", type=int, default=None, help="stop after N days for testing")
    parser.add_argument("--from-day", default=None, help="only run days strictly after YYYY-MM-DD (incremental mode)")
    parser.add_argument("--out-dir", default=None)
    parser.add_argument("--reset", action="store_true", help="ignore prior _running.md and per-day cache")
    args = parser.parse_args()

    # Headers are no longer assembled here — chat_call() picks them up from
    # the active provider on every call. Kept as an empty dict because
    # run_day still accepts it positionally for backward compat.
    headers: dict = {}

    conn = sqlite3.connect(CORPUS_DB)
    conn.row_factory = sqlite3.Row
    where = ["s.is_subagent = 0", substantive_filter("s")]
    params: list = []
    if args.project:
        source_repo = _tracked_source_repo(args.project)
        if source_repo:
            pred, pred_params = _project_dir_predicate(source_repo)
            where.append(pred)
            params.extend(pred_params)
        else:
            where.append("s.project_dir = ?")
            params.append(args.project)
    rows = conn.execute(
        f"""
        SELECT substr(p.timestamp, 1, 10) AS day, COUNT(*) AS n
        FROM prompts p JOIN sessions s ON p.session_id = s.session_id
        WHERE {' AND '.join(where)}
        GROUP BY day
        ORDER BY day
        """,
        params,
    ).fetchall()
    days = [(r["day"], r["n"]) for r in rows if r["day"]]
    if args.from_day:
        days = [(d, n) for d, n in days if d > args.from_day]
        if not days:
            print(f"No days strictly after {args.from_day} — already up to date.", flush=True)
            return
    if args.limit_days:
        days = days[: args.limit_days]

    out_dir = Path(args.out_dir) if args.out_dir else (ANALYSES_DIR / (args.project or "all"))
    out_dir.mkdir(parents=True, exist_ok=True)
    running_path = out_dir / "_running.md"
    prior = "" if args.reset else (running_path.read_text() if running_path.exists() else "")

    from watchmen.agent import provider_banner
    print(f"  {provider_banner(model=args.model)}", flush=True)
    print(f"Running on {len(days)} days, model={args.model}, output={out_dir}", flush=True)
    if prior:
        print(f"  resuming from prior thesis ({len(prior)} chars)", flush=True)

    with httpx.Client(timeout=300.0) as client:
        for day, count in days:
            day_path = out_dir / f"{day}.md"
            if day_path.exists() and not args.reset:
                prior = day_path.read_text()
                print(f"  [{day}] cached ({count} prompts)", flush=True)
                continue
            print(f"  [{day}] {count} prompts...", end=" ", flush=True)
            t0 = time.time()
            try:
                md = run_day(client, headers, args.model, day, args.project, prior)
            except Exception as e:
                print(f"FAILED: {e}", flush=True)
                continue
            elapsed = time.time() - t0
            if md:
                day_path.write_text(md)
                running_path.write_text(md)
                prior = md
                print(f"done in {elapsed:.1f}s ({len(md)} chars)", flush=True)
            else:
                print(f"no output in {elapsed:.1f}s", flush=True)

    print(f"\nFinal thesis: {running_path}")


if __name__ == "__main__":
    main()
