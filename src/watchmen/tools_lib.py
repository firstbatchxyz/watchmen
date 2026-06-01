"""Shared tool implementations for watchmen agents.

Each function returns a string (or json-serializable). Path-safe (no traversal out of scoped roots).
Scoping: tools are bound to a (corpus_db, source_repo, bundle_root, project_key) tuple via
make_tools(...) which returns a dict of (specs, handlers) ready to pass to Agent().
"""

import json
import sqlite3
from pathlib import Path

from watchmen.paths import ANALYSES_DIR, CORPUS_DB, BUNDLES_DIR

ROOT = Path(__file__).parent


# ─── Raw implementations (bound by make_tools) ─────────────────────────────

def query_corpus(sql: str, max_rows: int = 50) -> str:
    if not sql.strip().lower().startswith("select"):
        return "ERROR: only SELECT statements allowed"
    conn = sqlite3.connect(CORPUS_DB)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(sql).fetchmany(max_rows)
    except sqlite3.Error as e:
        return f"ERROR: {e}"
    return json.dumps([dict(r) for r in rows], default=str, indent=2)


def read_session_full(session_id: str, max_chars: int = 30000) -> str:
    conn = sqlite3.connect(CORPUS_DB)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT transcript_path FROM sessions WHERE session_id = ?", (session_id,)
    ).fetchone()
    if not row or not row["transcript_path"]:
        return f"session not found: {session_id}"
    parts: list[str] = []
    total = 0
    try:
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
    except FileNotFoundError:
        return f"transcript not found at {row['transcript_path']}"
    return "\n".join(parts) or "(empty)"


def read_thesis(project_key: str, section: str | None = None) -> str:
    """Read analyses/<project>/_running.md (full or one section by ## heading match)."""
    thesis_dir = ANALYSES_DIR / project_key
    if not thesis_dir.is_dir():
        return f"ERROR: thesis dir not found for project key: {project_key}"
    running = thesis_dir / "_running.md"
    if not running.exists():
        return f"ERROR: _running.md not found in {thesis_dir}"
    content = running.read_text(encoding="utf-8")
    if section is None:
        return content[:50000]
    target = section.lower().strip()
    out: list[str] = []
    in_section = False
    for line in content.splitlines():
        if line.startswith("## "):
            heading = line[3:].lower()
            if target in heading:
                in_section = True
                out.append(line)
                continue
            if in_section:
                break
            in_section = False
        elif in_section:
            out.append(line)
    return "\n".join(out) if out else f"section '{section}' not found"


def _resolve_safe(base: Path, sub_path: str) -> Path | None:
    """Return resolved path if it stays within base, else None."""
    target = (base / sub_path).resolve()
    try:
        target.relative_to(base.resolve())
        return target
    except ValueError:
        return None


def make_tools(*, source_repo: str, project_key: str) -> tuple[list[dict], dict]:
    """Bind a tool set to a project. Returns (tool_specs, handler_dict)."""

    repo_root = Path(source_repo).expanduser()
    bundle_root = BUNDLES_DIR / project_key

    # ── handlers ───────────────────────────────────────────────────────────

    def list_repo_files(pattern: str = "*", max_results: int = 100) -> str:
        if not repo_root.exists():
            return f"ERROR: source repo not found: {repo_root}"
        matches: list[str] = []
        for p in repo_root.rglob(pattern):
            if p.is_file() and ".git" not in p.parts and "node_modules" not in p.parts:
                matches.append(str(p.relative_to(repo_root)))
                if len(matches) >= max_results:
                    break
        return json.dumps(matches)

    def read_repo_file(file_path: str, max_chars: int = 20000) -> str:
        target = _resolve_safe(repo_root, file_path)
        if target is None:
            return "ERROR: path traversal blocked"
        if not target.exists() or not target.is_file():
            return f"ERROR: file not found: {file_path}"
        try:
            content = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return f"ERROR: binary file: {file_path}"
        if len(content) > max_chars:
            content = content[:max_chars] + f"\n[... {len(content) - max_chars} chars truncated ...]"
        return content

    def read_thesis_section(section: str = "") -> str:
        return read_thesis(project_key, section or None)

    def write_bundle_file(file_path: str, content: str) -> str:
        target = _resolve_safe(bundle_root, file_path)
        if target is None:
            return "ERROR: path traversal blocked"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"wrote: bundles/{project_key}/{file_path} ({len(content)} chars)"

    def list_bundle_files(subdir: str = "") -> str:
        base = bundle_root if not subdir else _resolve_safe(bundle_root, subdir)
        if base is None:
            return "ERROR: path traversal blocked"
        if not base.exists():
            return "[]"
        files = sorted(str(p.relative_to(bundle_root)) for p in base.rglob("*") if p.is_file())
        return json.dumps(files)

    def read_bundle_file(file_path: str, max_chars: int = 20000) -> str:
        target = _resolve_safe(bundle_root, file_path)
        if target is None:
            return "ERROR: path traversal blocked"
        if not target.exists():
            return f"ERROR: not found: {file_path}"
        content = target.read_text(encoding="utf-8")
        if len(content) > max_chars:
            content = content[:max_chars] + "\n[... truncated ...]"
        return content

    def append_curation_log(entry: str) -> str:
        target = bundle_root / "_curation_log.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a", encoding="utf-8") as f:
            import time
            f.write(f"\n## {time.strftime('%Y-%m-%d %H:%M:%S')}\n{entry}\n")
        return "logged"

    from watchmen.analyze import tool_fetch_pr_status

    def fetch_pr_status(repo: str, pr_number: int, host: str | None = None) -> str:
        return tool_fetch_pr_status(repo, pr_number, host)

    handlers = {
        "query_corpus": query_corpus,
        "read_session_full": read_session_full,
        "read_thesis_section": read_thesis_section,
        "list_repo_files": list_repo_files,
        "read_repo_file": read_repo_file,
        "write_bundle_file": write_bundle_file,
        "list_bundle_files": list_bundle_files,
        "read_bundle_file": read_bundle_file,
        "append_curation_log": append_curation_log,
        "fetch_pr_status": fetch_pr_status,
    }

    # ── specs ──────────────────────────────────────────────────────────────

    specs = [
        {"type": "function", "function": {
            "name": "query_corpus",
            "description": ("Run SELECT against corpus.db. Tables: sessions, prompts, tool_calls. "
                            "Same schema as analyzer."),
            "parameters": {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]},
        }},
        {"type": "function", "function": {
            "name": "read_session_full",
            "description": "Rendered transcript for a session (~30k chars).",
            "parameters": {"type": "object", "properties": {"session_id": {"type": "string"}}, "required": ["session_id"]},
        }},
        {"type": "function", "function": {
            "name": "read_thesis_section",
            "description": (f"Read the longitudinal thesis for project '{project_key}'. "
                            "Pass section name (e.g. 'Workflow archetypes', 'Skill candidates', 'Notable sessions') "
                            "or empty string for the full thesis."),
            "parameters": {"type": "object", "properties": {"section": {"type": "string"}}, "required": []},
        }},
        {"type": "function", "function": {
            "name": "list_repo_files",
            "description": (f"List files in the source repo ({repo_root}). Glob pattern (e.g. '*.py', "
                            "'**/*.py', 'scripts/*'). Returns up to 100 paths relative to repo root."),
            "parameters": {"type": "object", "properties": {
                "pattern": {"type": "string", "description": "glob, default '*'"},
                "max_results": {"type": "integer", "description": "default 100"},
            }, "required": []},
        }},
        {"type": "function", "function": {
            "name": "read_repo_file",
            "description": "Read a file from the source repo (relative path). ~20k chars max.",
            "parameters": {"type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"]},
        }},
        {"type": "function", "function": {
            "name": "write_bundle_file",
            "description": (f"Write a file under bundles/{project_key}/. Creates parent dirs. "
                            "Use for SKILL.md, scripts/*, references/*, CLAUDE.md, _index.md."),
            "parameters": {"type": "object", "properties": {
                "file_path": {"type": "string", "description": "relative path inside bundles/<project>/"},
                "content": {"type": "string"},
            }, "required": ["file_path", "content"]},
        }},
        {"type": "function", "function": {
            "name": "list_bundle_files",
            "description": "List files written so far under bundles/<project>/. Optional subdir filter.",
            "parameters": {"type": "object", "properties": {"subdir": {"type": "string"}}, "required": []},
        }},
        {"type": "function", "function": {
            "name": "read_bundle_file",
            "description": "Read a file you previously wrote under bundles/<project>/.",
            "parameters": {"type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"]},
        }},
        {"type": "function", "function": {
            "name": "append_curation_log",
            "description": "Append a timestamped entry to _curation_log.md (decisions, critic feedback, refinements).",
            "parameters": {"type": "object", "properties": {"entry": {"type": "string"}}, "required": ["entry"]},
        }},
        {"type": "function", "function": {
            "name": "fetch_pr_status",
            "description": (
                "Look up a pull request's review state and latest comments. Use when a session opened "
                "or updated a PR (visible in agent tool calls like create_pull_request, gh pr create, "
                "or kai_create_pull_request). Returns reviews (APPROVED / CHANGES_REQUESTED / "
                "COMMENTED) and the latest comments — these are the maintainer's feedback on the "
                "agent's work."
            ),
            "parameters": {"type": "object", "properties": {
                "repo": {"type": "string", "description": "'owner/name' or a full repo/PR URL"},
                "pr_number": {"type": "integer"},
                "host": {"type": "string", "description": "Optional non-github host (e.g. a Forgejo domain). Defaults to github.com."},
            }, "required": ["repo", "pr_number"]},
        }},
    ]

    return specs, handlers
