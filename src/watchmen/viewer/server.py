"""watchmen viewer — local FastAPI dashboard for browsing analyses + skill bundles + CLAUDE.md."""

import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import bleach
import markdown as md
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from watchmen.paths import ANALYSES_DIR, BUNDLES_DIR, CORPUS_DB, STATE_DB
from watchmen.util import (
    ADAPTER_SHORT,
    BLOCKLIST_FILE,
    PINNED_FILE,
    read_skill_list,
    write_skill_list,
)
from watchmen.viewer import actions as wm_actions

ROOT = Path(__file__).parent.parent  # src/watchmen/
ANALYSES = ANALYSES_DIR
BUNDLES = BUNDLES_DIR
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


MD_EXTENSIONS = ["fenced_code", "tables", "codehilite", "toc", "sane_lists", "nl2br"]
MD_CONFIG = {"codehilite": {"css_class": "codehilite", "guess_lang": True}}
MD_ALLOWED_TAGS = set(bleach.sanitizer.ALLOWED_TAGS) | {
    "a", "abbr", "article", "blockquote", "br", "code", "dd", "div", "dl", "dt",
    "h1", "h2", "h3", "h4", "h5", "h6", "hr", "img", "li", "ol", "p", "pre",
    "span", "strong", "table", "tbody", "td", "th", "thead", "tr", "ul",
}
MD_ALLOWED_ATTRS = {
    **bleach.sanitizer.ALLOWED_ATTRIBUTES,
    "*": ["class", "id"],
    "a": ["href", "title", "rel"],
    "img": ["src", "alt", "title"],
    "td": ["align"],
    "th": ["align"],
}


def render_md(text: str) -> str:
    html = md.markdown(text, extensions=MD_EXTENSIONS, extension_configs=MD_CONFIG)
    return bleach.clean(
        html,
        tags=MD_ALLOWED_TAGS,
        attributes=MD_ALLOWED_ATTRS,
        protocols=["http", "https", "mailto"],
        strip=True,
    )


def _db():
    if not STATE_DB.exists():
        return None
    c = sqlite3.connect(STATE_DB)
    c.row_factory = sqlite3.Row
    return c


def list_tracked_projects() -> list[dict]:
    c = _db()
    if not c:
        return []
    rows = c.execute("SELECT * FROM projects ORDER BY project_key").fetchall()
    c.close()
    return [dict(r) for r in rows]


def get_project_meta(project_key: str) -> dict | None:
    c = _db()
    if not c:
        return None
    row = c.execute("SELECT * FROM projects WHERE project_key = ?", (project_key,)).fetchone()
    c.close()
    return dict(row) if row else None


def list_skills(project_key: str) -> list[dict]:
    skills_dir = BUNDLES / project_key / "skills"
    if not skills_dir.exists():
        return []
    out = []
    for d in sorted(skills_dir.iterdir()):
        if not d.is_dir():
            continue
        skill_md_path = d / "SKILL.md"
        description = ""
        if skill_md_path.exists():
            text = skill_md_path.read_text(encoding="utf-8", errors="replace")
            for line in text.splitlines():
                if line.startswith("description:"):
                    description = line.split(":", 1)[1].strip()
                    break
                if line.startswith("# "):
                    description = line[2:].strip()
                    break
        scripts_dir = d / "scripts"
        script_count = sum(1 for f in scripts_dir.glob("*") if f.is_file()) if scripts_dir.exists() else 0
        out.append({
            "slug": d.name,
            "description": description,
            "script_count": script_count,
            "has_skill_md": skill_md_path.exists(),
        })
    return out


def list_thesis_days(project_key: str) -> list[dict]:
    d = ANALYSES / project_key
    if not d.exists():
        return []
    days = []
    for f in sorted(d.glob("20*.md")):
        days.append({
            "day": f.stem,
            "size": f.stat().st_size,
        })
    return days


def list_recent_runs(limit: int = 30) -> list[dict]:
    c = _db()
    if not c:
        return []
    rows = c.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    c.close()
    return [dict(r) for r in rows]


def _curation_log_excerpt(proj_dir: Path, skill_slug: str, skill_name: str) -> str:
    """Pull the relevant block from _curation_log.md for a given skill.
    Mirrors commands.inspect._curation_log_excerpt; copied here to keep the
    viewer module standalone (no cross-imports into commands/)."""
    log = proj_dir / "_curation_log.md"
    if not log.exists():
        return ""
    lines = log.read_text().splitlines()
    needles = (skill_slug.lower(), skill_name.lower())
    for i, line in enumerate(lines):
        if line.startswith("## ") and any(n in line.lower() for n in needles):
            body = [line]
            for j in range(i + 1, min(len(lines), i + 30)):
                if lines[j].startswith("## "):
                    break
                body.append(lines[j])
            return "\n".join(body).strip()
    return ""


def get_skill_provenance(project_key: str, skill_slug: str) -> dict:
    """Why does this skill exist? Returns the candidate's stated triggers,
    source files (with existence check), source sessions cross-referenced
    against corpus.db, and the curator's rationale excerpt.

    Returns {} when there's no candidate match — callers should treat that
    as "no provenance available" and skip the section in the template."""
    proj_dir = BUNDLES / project_key
    candidates_path = proj_dir / "_candidates.json"
    if not candidates_path.exists():
        return {}
    try:
        cands = json.loads(candidates_path.read_text())
    except json.JSONDecodeError:
        return {}
    match = next(
        (c for c in cands if c.get("slug") == skill_slug
         or c.get("name", "").lower() == skill_slug.lower()),
        None,
    )
    if not match:
        return {}

    name = match.get("name", skill_slug)
    slug = match.get("slug", skill_slug)
    when_to_use = match.get("when_to_use") or []
    if isinstance(when_to_use, str):
        when_to_use = [when_to_use]
    source_files = match.get("source_files") or []
    source_files_resolved = [
        {"path": f, "exists": Path(f).exists()} for f in source_files
    ]

    # Cross-reference session ids with corpus.db. Tolerates free-form
    # labels (codex/pi sessions sometimes include annotations) by matching
    # on the first whitespace/paren-delimited token via LIKE prefix.
    sessions: list[dict] = []
    raw_ids = match.get("session_ids") or []
    if raw_ids and CORPUS_DB.exists():
        try:
            cc = sqlite3.connect(str(CORPUS_DB))
            cc.row_factory = sqlite3.Row
            try:
                cc.execute("SELECT 1 FROM sessions LIMIT 1")
            except sqlite3.OperationalError:
                cc.close()
                cc = None
        except sqlite3.Error:
            cc = None
        if cc is not None:
            for sid in raw_ids:
                short = (
                    sid.split()[0].split("(")[0].strip()
                    if isinstance(sid, str) else str(sid)
                )
                row = cc.execute(
                    """SELECT s.session_id, s.agent, s.started_at,
                              (SELECT text FROM prompts
                               WHERE session_id = s.session_id
                               ORDER BY rowid LIMIT 1) AS first_prompt
                       FROM sessions s
                       WHERE s.session_id LIKE ? || '%' LIMIT 1""",
                    (short,),
                ).fetchone()
                if row:
                    snippet = (row["first_prompt"] or "").replace("\n", " ")[:120]
                    sessions.append({
                        "id": short[:14],
                        "agent": ADAPTER_SHORT.get(row["agent"], row["agent"]),
                        "agent_full": row["agent"],
                        "date": (row["started_at"] or "")[:10],
                        "snippet": snippet,
                        "found": True,
                    })
                else:
                    sessions.append({
                        "id": short[:14],
                        "agent": "?",
                        "agent_full": "",
                        "date": "",
                        "snippet": str(sid)[:120],
                        "found": False,
                    })
            cc.close()

    excerpt = _curation_log_excerpt(proj_dir, slug, name)
    return {
        "name": name,
        "slug": slug,
        "description": match.get("description", ""),
        "when_to_use": list(when_to_use)[:8],
        "when_to_use_more": max(0, len(when_to_use) - 8),
        "source_files": source_files_resolved,
        "sessions": sessions,
        "curator_excerpt": excerpt,
    }


def get_skill_status(project_key: str, skill_slug: str) -> dict:
    """Pinned / blocked status of a skill. Drives the control buttons:
    Pin vs Unpin label, Drop confirm prompt, restore-from-blocklist hint."""
    pinned = read_skill_list(project_key, PINNED_FILE)
    blocked = read_skill_list(project_key, BLOCKLIST_FILE)
    return {
        "pinned": skill_slug in pinned,
        "blocked": skill_slug in blocked,
    }


# ─── App ────────────────────────────────────────────────────────────────────

app = FastAPI(title="watchmen viewer")


@app.get("/healthz", response_class=PlainTextResponse)
def healthz() -> str:
    return "ok"


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    projects = list_tracked_projects()
    summaries = []
    for p in projects:
        skills = list_skills(p["project_key"])
        days = list_thesis_days(p["project_key"])
        claude_md = BUNDLES / p["project_key"] / "CLAUDE.md"
        summaries.append({
            **p,
            "skill_count": len(skills),
            "thesis_day_count": len(days),
            "claude_md_size": claude_md.stat().st_size if claude_md.exists() else 0,
        })
    runs = list_recent_runs(limit=10)

    # Read the latest CHANGELOG.md entry + current version so dashboard.html
    # can render a "What's new in vX.Y" banner. JS dismisses + remembers in
    # localStorage so each new release announces itself exactly once per
    # user-browser. Falls back to no banner when the file is missing.
    changelog_version: str | None = None
    changelog_body_html: str | None = None
    try:
        # cli still owns _version + _parse_changelog (kept there because
        # they're tightly coupled to first-run release-notes notification).
        from watchmen import cli as _cli
        from watchmen.util import find_changelog
        changelog_version = _cli._version()
        changelog_path = find_changelog()
        if changelog_path is not None:
            entries = _cli._parse_changelog(changelog_path.read_text())
            for v, body in entries:
                if v == changelog_version:
                    changelog_body_html = md.markdown(body, extensions=["fenced_code"])
                    break
    except Exception:
        pass

    return TEMPLATES.TemplateResponse(request, "dashboard.html", {
        "projects": summaries,
        "runs": runs,
        "changelog_version": changelog_version,
        "changelog_body_html": changelog_body_html,
        "next_actions": wm_actions.next_best_actions(limit=5),
        "active_web_runs": [r for r in wm_actions.list_runs(limit=5) if r["alive"]],
    })


@app.get("/p/{project_key}", response_class=HTMLResponse)
def project_page(request: Request, project_key: str):
    proj = get_project_meta(project_key)
    if not proj:
        raise HTTPException(404, f"project {project_key} not tracked")
    claude_md_path = BUNDLES / project_key / "CLAUDE.md"
    claude_md_html = render_md(claude_md_path.read_text(encoding="utf-8")) if claude_md_path.exists() else None
    skills = list_skills(project_key)
    days = list_thesis_days(project_key)
    return TEMPLATES.TemplateResponse(request, "project.html", {
        "project": proj,
        "claude_md": claude_md_html,
        "skills": skills,
        "thesis_days": days,
        "next_actions": wm_actions.next_best_actions(project_key=project_key, limit=4),
        "active_web_runs": [
            r for r in wm_actions.list_runs(limit=10)
            if r["alive"] and r.get("project_key") == project_key
        ],
    })


@app.get("/p/{project_key}/skills/{skill_slug}", response_class=HTMLResponse)
def skill_page(request: Request, project_key: str, skill_slug: str):
    skill_dir = BUNDLES / project_key / "skills" / skill_slug
    if not skill_dir.exists():
        raise HTTPException(404, f"skill {skill_slug} not found")
    skill_md_path = skill_dir / "SKILL.md"
    skill_md_html = render_md(skill_md_path.read_text(encoding="utf-8")) if skill_md_path.exists() else None
    files = []
    for f in sorted(skill_dir.rglob("*")):
        if f.is_file() and f.name != "SKILL.md":
            files.append({
                "rel": str(f.relative_to(skill_dir)),
                "size": f.stat().st_size,
            })
    provenance = get_skill_provenance(project_key, skill_slug)
    status = get_skill_status(project_key, skill_slug)
    return TEMPLATES.TemplateResponse(request, "skill.html", {
        "project_key": project_key,
        "skill_slug": skill_slug,
        "skill_md": skill_md_html,
        "files": files,
        "provenance": provenance,
        "status": status,
    })


# ─── Skill control endpoints ────────────────────────────────────────────────
# Mutate pin/blocklist state and (for drop) remove the bundle directory.
# Plain POST → 303 redirect pattern: keeps the browser back-button sane and
# avoids needing JSON fetch. CLI parity comes from the shared util helpers.

def _skill_or_404(project_key: str, skill_slug: str) -> Path:
    skill_dir = BUNDLES / project_key / "skills" / skill_slug
    if not skill_dir.exists():
        raise HTTPException(404, f"skill {skill_slug} not found")
    return skill_dir


@app.post("/p/{project_key}/skills/{skill_slug}/pin")
def skill_pin(project_key: str, skill_slug: str):
    _skill_or_404(project_key, skill_slug)
    pinned = read_skill_list(project_key, PINNED_FILE)
    pinned.add(skill_slug)
    write_skill_list(project_key, PINNED_FILE, pinned)
    return RedirectResponse(
        url=f"/p/{project_key}/skills/{skill_slug}", status_code=303
    )


@app.post("/p/{project_key}/skills/{skill_slug}/unpin")
def skill_unpin(project_key: str, skill_slug: str):
    pinned = read_skill_list(project_key, PINNED_FILE)
    pinned.discard(skill_slug)
    write_skill_list(project_key, PINNED_FILE, pinned)
    return RedirectResponse(
        url=f"/p/{project_key}/skills/{skill_slug}", status_code=303
    )


@app.post("/p/{project_key}/skills/{skill_slug}/drop")
def skill_drop(project_key: str, skill_slug: str):
    """Add to blocklist + remove the bundle directory. After this the skill
    page 404s — so redirect to the project page instead."""
    skill_dir = _skill_or_404(project_key, skill_slug)
    blocklist = read_skill_list(project_key, BLOCKLIST_FILE)
    blocklist.add(skill_slug)
    write_skill_list(project_key, BLOCKLIST_FILE, blocklist)
    if skill_dir.exists():
        shutil.rmtree(skill_dir)
    return RedirectResponse(url=f"/p/{project_key}", status_code=303)


@app.post("/p/{project_key}/skills/{skill_slug}/restore")
def skill_restore(project_key: str, skill_slug: str):
    """Remove from blocklist; the next curator run can re-propose the slug.
    Used on a still-present bundle that was *marked* blocked but not dropped."""
    blocklist = read_skill_list(project_key, BLOCKLIST_FILE)
    blocklist.discard(skill_slug)
    write_skill_list(project_key, BLOCKLIST_FILE, blocklist)
    return RedirectResponse(
        url=f"/p/{project_key}/skills/{skill_slug}", status_code=303
    )


# ─── Web-triggered runs ──────────────────────────────────────────────────────
# Lets users press "Analyze now" / "Curate now" buttons in the action banner.
# Spawns a detached subprocess; the page redirects to /actions/run/<id> which
# tails the log file until the process exits. State files live under
# ~/.watchmen/web-runs/ alongside the daemon's existing storage.


@app.post("/actions/run")
async def actions_run(request: Request):
    """Spawn a CLI subprocess. Parses the urlencoded body manually to avoid
    pulling in python-multipart (FastAPI's Form(...) and request.form() both
    require it; we don't otherwise need form upload features)."""
    from urllib.parse import parse_qs
    raw = (await request.body()).decode("utf-8", errors="replace")
    fields = parse_qs(raw, keep_blank_values=False)
    action = (fields.get("action", [""])[0]).strip()
    project_key = (fields.get("project_key", [""])[0]).strip()
    if not action or not project_key:
        raise HTTPException(400, "missing 'action' or 'project_key' form field")
    try:
        meta = wm_actions.start_run(action, project_key)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    return RedirectResponse(url=f"/actions/run/{meta['id']}", status_code=303)


@app.get("/actions/run/{run_id}", response_class=HTMLResponse)
def actions_run_view(request: Request, run_id: str):
    meta = wm_actions.get_run(run_id)
    if not meta:
        raise HTTPException(404, f"run {run_id} not found")
    return TEMPLATES.TemplateResponse(request, "action_run.html", {
        "run": meta,
    })


@app.get("/p/{project_key}/skills/{skill_slug}/files/{file_rel:path}", response_class=HTMLResponse)
def skill_file(request: Request, project_key: str, skill_slug: str, file_rel: str):
    skill_dir = BUNDLES / project_key / "skills" / skill_slug
    target = (skill_dir / file_rel).resolve()
    try:
        target.relative_to(skill_dir.resolve())
    except ValueError:
        raise HTTPException(400, "path traversal blocked")
    if not target.exists():
        raise HTTPException(404)
    text = target.read_text(encoding="utf-8", errors="replace")
    suffix = target.suffix.lstrip(".")
    lang_map = {"py": "python", "sh": "bash", "md": "markdown", "yml": "yaml", "yaml": "yaml", "json": "json", "js": "javascript", "ts": "typescript", "tsx": "tsx", "toml": "toml"}
    lang = lang_map.get(suffix, "text")
    fenced = f"```{lang}\n{text}\n```"
    rendered = render_md(fenced)
    return TEMPLATES.TemplateResponse(request, "file.html", {
        "project_key": project_key,
        "skill_slug": skill_slug,
        "file_rel": file_rel,
        "file_html": rendered,
        "byte_size": len(text),
    })


@app.get("/p/{project_key}/thesis", response_class=HTMLResponse)
def thesis_index(request: Request, project_key: str):
    days = list_thesis_days(project_key)
    running = ANALYSES / project_key / "_running.md"
    running_html = render_md(running.read_text(encoding="utf-8")) if running.exists() else None
    return TEMPLATES.TemplateResponse(request, "thesis.html", {
        "project_key": project_key,
        "days": days,
        "running_html": running_html,
    })


@app.get("/p/{project_key}/thesis/{day}", response_class=HTMLResponse)
def thesis_day(request: Request, project_key: str, day: str):
    f = ANALYSES / project_key / f"{day}.md"
    if not f.exists():
        raise HTTPException(404)
    return TEMPLATES.TemplateResponse(request, "thesis_day.html", {
        "project_key": project_key,
        "day": day,
        "html": render_md(f.read_text(encoding="utf-8")),
    })


@app.get("/runs", response_class=HTMLResponse)
def runs_page(request: Request):
    runs = list_recent_runs(limit=200)
    return TEMPLATES.TemplateResponse(request, "runs.html", {
        "runs": runs,
    })


@app.get("/insights", response_class=HTMLResponse)
def insights_page(request: Request):
    """HTML version of `watchmen insights`. Same static aggregation +
    cached deep digest as the CLI, but with richer charts (per-repo
    activity sparklines, top erroring tools, frustration markers,
    aggregate hour-of-day heatmap) that don't fit a terminal."""
    import json as _json
    from watchmen import metrics as _metrics
    # Friction-signal + adapter helpers moved to watchmen.util during the
    # Phase 3 split; the digest cache reader lives in commands.insights.
    # The viewer pulls from the canonical sources rather than re-reaching
    # into cli.py.
    from watchmen import state as _state
    from watchmen.util import adapter_breakdown, repo_friction_signals

    _state.init_db()
    projects = _state.list_projects()
    base = BUNDLES

    # Adapter totals across the whole corpus.
    adapter_totals: dict[str, int] = {}
    if CORPUS_DB.exists():
        cc = sqlite3.connect(str(CORPUS_DB))
        for agent, n in cc.execute(
            "SELECT agent, COUNT(*) FROM sessions WHERE is_subagent = 0 GROUP BY agent ORDER BY 2 DESC"
        ).fetchall():
            adapter_totals[agent] = n
        cc.close()

    # Per-repo rows with sparklines + friction signals.
    repos: list[dict] = []
    for p in projects:
        key = p["project_key"]
        skills_dir = base / key / "skills"
        skills_n = sum(1 for d in skills_dir.iterdir() if d.is_dir()) if skills_dir.exists() else 0
        pending_dir = base / key / "_pending"
        pending_n = sum(1 for d in pending_dir.iterdir() if d.is_dir()) if pending_dir.exists() else 0
        adapter = adapter_breakdown(key)
        tool_errors, top_error_tools, frust_count, frust_samples = repo_friction_signals(key)
        daily = _metrics.daily_metrics(key, days=30) or []
        sess_chronological = list(reversed(daily))
        try:
            prog = _state.get_project_progress(key)
            pending_prompts = prog.get("new_prompts_since_last_analysis", 0) or 0
        except Exception:
            pending_prompts = 0
        repos.append({
            "key": key,
            "skills_n": skills_n,
            "pending_n": pending_n,
            "adapter": adapter,
            "tool_errors": tool_errors,
            "top_error_tools": top_error_tools,
            "frust_count": frust_count,
            "frust_samples": frust_samples,
            # Per-repo sparkline payload — same shape as the area-chart helper
            # in base.html consumes. Renders client-side with the shared theme
            # so per-row sparks match the profile-card sparks visually.
            "sess_spark_data": [
                {"date": r["date"], "value": r["sessions"]} for r in sess_chronological
            ],
            "pending_prompts": pending_prompts,
            "total_sess": sum(adapter.values()),
        })
    repos.sort(key=lambda r: (-r["skills_n"], -r["total_sess"]))

    # Cross-repo candidate-slug overlaps.
    pattern_idx: dict[str, list[tuple[str, str]]] = {}
    for p in projects:
        key = p["project_key"]
        cand_path = base / key / "_candidates.json"
        skills_dir = base / key / "skills"
        existing = {d.name for d in skills_dir.iterdir() if d.is_dir()} if skills_dir.exists() else set()
        if not cand_path.exists():
            continue
        try:
            cands = _json.loads(cand_path.read_text())
        except Exception:
            continue
        for c in cands:
            slug = c.get("slug")
            if not slug:
                continue
            status = "curated" if slug in existing else "candidate"
            pattern_idx.setdefault(slug, []).append((key, status))
    cross = [(slug, hits) for slug, hits in pattern_idx.items() if len(hits) >= 2]
    cross.sort(key=lambda x: (-len(x[1]), x[0]))

    untapped = [(r["key"], r["total_sess"]) for r in repos if r["skills_n"] == 0 and r["total_sess"] > 0]
    untapped.sort(key=lambda x: -x[1])

    # Aggregate per-repo charts for cross-repo comparison. Both are bar
    # charts; payloads pass through the shared ECharts helper in base.html.
    frust_chart_data = [
        {"label": k, "value": n}
        for k, n in sorted([(r["key"], r["frust_count"]) for r in repos if r["frust_count"] > 0],
                           key=lambda x: -x[1])[:8]
    ]
    errors_chart_data = [
        {"label": k, "value": n}
        for k, n in sorted([(r["key"], r["tool_errors"]) for r in repos if r["tool_errors"] > 0],
                           key=lambda x: -x[1])[:8]
    ]

    # Aggregate metrics (rollup window + heatmap) — reuse what /metrics builds.
    aggregate_rows = _metrics.daily_metrics_all(days=30, tracked_only=False)
    last7 = _metrics.summarize_window(aggregate_rows, 7)
    last30 = _metrics.summarize_window(aggregate_rows, 30)
    series = list(reversed(aggregate_rows))
    def _series_insights(key: str) -> list[dict]:
        return [{"date": r["date"], "value": r[key]} for r in series]
    sparks_data = {
        "sessions":    _series_insights("sessions"),
        "prompts":     _series_insights("prompts"),
        "tool_errors": _series_insights("tool_errors"),
        "cost_usd":    _series_insights("cost_usd"),
    }
    hour_dow = _metrics.activity_by_hour_dow_all(days=90, tracked_only=False)
    hour_dow_data = {
        "points": [
            {"hour": h, "dow": d, "value": hour_dow[d][h]}
            for d in range(7) for h in range(24)
        ],
    }

    # Latest cached deep digest from ~/.watchmen/insights/.
    digest_html = None
    digest_meta: dict = {}
    cmp_narrative_html = ""
    cmp_narrative_meta: dict = {}
    try:
        from watchmen.commands.insights import (
            _latest_digest_path,
            _read_digest_metadata,
            _latest_cross_agent_narrative,
        )
        latest = _latest_digest_path()
        if latest is not None:
            meta, body = _read_digest_metadata(latest)
            digest_meta = meta
            digest_html = render_md(body)
        # Cross-agent narrative — independent cache file, rendered inline
        # above the deep digest so users see the per-agent context first.
        loaded = _latest_cross_agent_narrative()
        if loaded:
            cmp_narrative_meta, cmp_body = loaded
            cmp_narrative_html = render_md(cmp_body)
    except Exception:
        digest_html = None

    return TEMPLATES.TemplateResponse(request, "insights.html", {
        "adapter_totals": adapter_totals,
        "total_sessions": sum(adapter_totals.values()),
        "repos": repos,
        "cross": cross,
        "untapped": untapped,
        "frust_chart_data": frust_chart_data,
        "errors_chart_data": errors_chart_data,
        "last7": last7,
        "last30": last30,
        "sparks_data": sparks_data,
        "hour_dow_data": hour_dow_data,
        "digest_html": digest_html,
        "digest_meta": digest_meta,
        "cmp_narrative_html": cmp_narrative_html,
        "cmp_narrative_meta": cmp_narrative_meta,
        "curated_count": sum(1 for r in repos if r["skills_n"] > 0),
        "n_projects": len(projects),
        "total_skills": sum(r["skills_n"] for r in repos),
        "total_pending": sum(r["pending_n"] for r in repos),
        "total_errors": sum(r["tool_errors"] for r in repos),
        "total_frustration": sum(r["frust_count"] for r in repos),
    })


@app.get("/metrics", response_class=HTMLResponse)
def metrics_all(request: Request, tracked: int = 0):
    from watchmen import metrics as _metrics
    tracked_only = bool(tracked)
    rows = _metrics.daily_metrics_all(days=30, tracked_only=tracked_only)
    last7 = _metrics.summarize_window(rows, 7)
    last30 = _metrics.summarize_window(rows, 30)
    series = list(reversed(rows))
    # Sparks now ship as raw {date, value} arrays — ECharts area-chart helper
    # in base.html mounts each one client-side with the same shadcn theme as
    # the profile card. One helper, one theme, every chart on the page.
    def _series(key: str) -> list[dict]:
        return [{"date": r["date"], "value": r[key]} for r in series]
    sparks_data = {
        "sessions":     _series("sessions"),
        "prompts":      _series("prompts"),
        "input_tokens": _series("input_tokens"),
        "output_tokens":_series("output_tokens"),
        "tool_errors":  _series("tool_errors"),
        "cost_usd":     _series("cost_usd"),
        "suggestions":  _series("suggestions_fired"),
    }
    # Calendar heatmap: pass raw [(date, count), ...] as JSON-friendly pairs.
    # Range derived from first/last date so ECharts' calendar coord system
    # can lay out exactly the weeks we have data for.
    calendar = _metrics.activity_calendar_all(weeks=26, tracked_only=tracked_only)
    calendar_data = {
        "points": [{"date": d, "value": int(n)} for d, n in calendar],
        "range":  [calendar[0][0], calendar[-1][0]] if calendar else None,
    }
    # Hour×DOW heatmap: hour_dow[day_of_week][hour] = count. ECharts heatmap
    # wants flat [hour, day, value] triples; client helper unpacks.
    hour_dow = _metrics.activity_by_hour_dow_all(days=90, tracked_only=tracked_only)
    hour_dow_data = {
        "points": [
            {"hour": h, "dow": d, "value": hour_dow[d][h]}
            for d in range(7) for h in range(24)
        ],
    }
    peaks = []
    flat = [(dow, hr, hour_dow[dow][hr]) for dow in range(7) for hr in range(24)]
    flat.sort(key=lambda t: t[2], reverse=True)
    if flat and flat[0][2] > 0:
        peak_dow, peak_hr, peak_n = flat[0]
        peaks = [["Sun","Mon","Tue","Wed","Thu","Fri","Sat"][peak_dow], f"{peak_hr:02d}:00", peak_n]
    per_project = _metrics.per_project_totals(days=30)
    tool_usage = _metrics.tool_usage(project_key=None, days=30, tracked_only=tracked_only)
    streak = _metrics.streak_stats(project_key=None, weeks=26, tracked_only=tracked_only)
    adapters = _metrics.adapter_breakdown_all(days=30, tracked_only=tracked_only)

    # Profile card lives at the top of /metrics. Tracked-only mode is a
    # numeric-aggregation filter; the card uses the full corpus (the
    # window comes from ?card_days=N, default 90 to match the user's
    # mental model of "the last few months").
    card_days = int(request.query_params.get("card_days", "90") or "90")
    card_days = max(7, min(card_days, 730))
    card_stats = _metrics.compute_card_stats(days=card_days)
    card_tier = _metrics.card_tier_colors(card_stats["rating"])
    # Companion visualizations: agent-mix donut, top-tools horizontal
    # bars, daily activity sparklines, attribute radar. All four are now
    # client-rendered ECharts mounts fed by JSON; the legend data is
    # reused for both the donut center label and the side-panel legend.
    card_donut_legend = _metrics.agent_donut_legend(card_stats["agents"])
    card_donut_data = [
        {"label": row["label"], "value": row["count"], "color": row["color"]}
        for row in card_donut_legend
    ]
    card_donut_center_value = sum(card_stats["agents"].values())
    top_tool_rows = card_stats.get("top_tools", [])[:5]
    card_top_tools_data = [
        {"label": name, "value": int(n)} for name, n in top_tool_rows
    ]
    # Radar payload: axis names (from CARD_AXES) + scaled values 0..100.
    # ECharts radar uses indicator.max as the outer ring, so we pass 100
    # and multiply the 0..1 stats["axes"] by 100 to fill the band system.
    _axes_raw = card_stats.get("axes") or {}
    card_radar_data = {
        "indicators": [{"name": a, "max": 100} for a in _metrics.CARD_AXES],
        "values": [round(_axes_raw.get(a, 0) * 100, 1) for a in _metrics.CARD_AXES],
    }
    # Legend explaining each axis so the radar isn't a "what do these words
    # mean" puzzle. Kept short — one line per axis, ordered to match the
    # radar's spoke order so the eye can connect axis → definition by
    # position. Caps mirrored from _CARD_CAPS in metrics.py for the "elite"
    # column so the user can see what the outer ring represents per axis.
    card_axis_legend = [
        {"name": "Throughput",  "desc": "Prompts per active day",          "elite": "40/d"},
        {"name": "Frugality",   "desc": "Cost per prompt (lower is better)", "elite": "≤ $0.04"},
        {"name": "Reliability", "desc": "Tool-call success rate",          "elite": "100%"},
        {"name": "Curiosity",   "desc": "Distinct tools you reach for",    "elite": "30 tools"},
        {"name": "Range",       "desc": "Distinct repos you work across",  "elite": "12 repos"},
        {"name": "Mastery",     "desc": "Curated skill bundles owned",     "elite": "25 skills"},
    ]
    # Daily activity series — slice from daily_metrics_all (already loaded
    # above) so we don't re-query corpus.db just for the sparklines.
    #
    # Activity data is now passed as raw JSON arrays to the template; the
    # client-side ECharts helper in base.html renders each one as a
    # shadcn-themed area chart with hover tooltips. Replaces the
    # server-rendered `sparkline_svg` strings that fed the old static layout.
    activity_window = _metrics.daily_metrics_all(days=card_days, tracked_only=False)
    activity_series = list(reversed(activity_window))
    card_activity_data = {
        "sessions":    [{"date": r["date"], "value": r["sessions"]}    for r in activity_series],
        "cost":        [{"date": r["date"], "value": r["cost_usd"]}    for r in activity_series],
        "tool_errors": [{"date": r["date"], "value": r["tool_errors"]} for r in activity_series],
    }

    # Cross-agent comparison: per-adapter facts (always available, pure SQL)
    # + LLM-synthesized narrative (cached in ~/.watchmen/insights/, written
    # by the digest pipeline). The narrative is None when the user hasn't
    # run `watchmen insights` yet OR when <2 adapters have meaningful data.
    # Whole section hides itself in the template when there's <2 adapters.
    cmp_facts = _metrics.agent_comparison_facts(days=card_days)
    cmp_narrative_html = ""
    cmp_narrative_meta: dict = {}
    try:
        from watchmen.commands.insights import _latest_cross_agent_narrative
        loaded = _latest_cross_agent_narrative()
        if loaded:
            cmp_narrative_meta, cmp_body = loaded
            cmp_narrative_html = render_md(cmp_body)
    except Exception:
        # Worst case: the narrative section just shows the facts table
        # without the LLM prose. Don't block the whole page.
        pass

    return TEMPLATES.TemplateResponse(request, "metrics_all.html", {
        "rows": rows,
        "last7": last7,
        "last30": last30,
        "sparks_data": sparks_data,
        "calendar_data": calendar_data,
        "hour_dow_data": hour_dow_data,
        "peaks": peaks,
        "per_project": per_project,
        "tracked_only": tracked_only,
        "tool_usage": tool_usage,
        "streak": streak,
        "adapters": adapters,
        "card_stats": card_stats,
        "card_tier": card_tier,
        "card_days": card_days,
        "card_donut_legend": card_donut_legend,
        "card_donut_data": card_donut_data,
        "card_donut_center_value": card_donut_center_value,
        "card_top_tools_data": card_top_tools_data,
        "card_radar_data": card_radar_data,
        "card_axis_legend": card_axis_legend,
        "card_activity_data": card_activity_data,
        "cmp_facts": cmp_facts,
        "cmp_narrative_html": cmp_narrative_html,
        "cmp_narrative_meta": cmp_narrative_meta,
    })


@app.get("/p/{project_key}/metrics", response_class=HTMLResponse)
def project_metrics(request: Request, project_key: str):
    from watchmen import metrics as _metrics
    rows = _metrics.daily_metrics(project_key, days=30)
    last7 = _metrics.summarize_window(rows, 7)
    last30 = _metrics.summarize_window(rows, 30)
    # Daily series in chronological order for sparklines (rows is newest-first).
    series = list(reversed(rows))
    def _series_pm(key: str) -> list[dict]:
        return [{"date": r["date"], "value": r[key]} for r in series]
    sparks_data = {
        "sessions":     _series_pm("sessions"),
        "prompts":      _series_pm("prompts"),
        "input_tokens": _series_pm("input_tokens"),
        "output_tokens":_series_pm("output_tokens"),
        "tool_errors":  _series_pm("tool_errors"),
        "cost_usd":     _series_pm("cost_usd"),
        "suggestions":  _series_pm("suggestions_fired"),
    }
    calendar = _metrics.activity_calendar(project_key, weeks=26)
    hour_dow = _metrics.activity_by_hour_dow(project_key, days=90)
    calendar_data = {
        "points": [{"date": d, "value": int(n)} for d, n in calendar],
        "range":  [calendar[0][0], calendar[-1][0]] if calendar else None,
    }
    hour_dow_data = {
        "points": [
            {"hour": h, "dow": d, "value": hour_dow[d][h]}
            for d in range(7) for h in range(24)
        ],
    }

    # Peak hour + day for the summary line under the heatmap
    peaks = []
    flat = [(dow, hr, hour_dow[dow][hr]) for dow in range(7) for hr in range(24)]
    flat.sort(key=lambda t: t[2], reverse=True)
    if flat and flat[0][2] > 0:
        peak_dow, peak_hr, peak_n = flat[0]
        peaks = [["Sun","Mon","Tue","Wed","Thu","Fri","Sat"][peak_dow], f"{peak_hr:02d}:00", peak_n]

    tool_usage = _metrics.tool_usage(project_key=project_key, days=30)
    streak = _metrics.streak_stats(project_key=project_key, weeks=26)

    return TEMPLATES.TemplateResponse(request, "metrics.html", {
        "project": get_project_meta(project_key) or {"project_key": project_key},
        "rows": rows,
        "last7": last7,
        "last30": last30,
        "sparks_data": sparks_data,
        "calendar_data": calendar_data,
        "hour_dow_data": hour_dow_data,
        "peaks": peaks,
        "tool_usage": tool_usage,
        "streak": streak,
    })


def _project_git_dir(project_key: str) -> Path | None:
    pdir = BUNDLES / project_key
    if not pdir.exists() or not (pdir / ".git").exists():
        return None
    return pdir


@app.get("/p/{project_key}/runs", response_class=HTMLResponse)
def project_runs(request: Request, project_key: str):
    pdir = _project_git_dir(project_key)
    if pdir is None:
        raise HTTPException(404, detail="no run history yet — curator hasn't committed anything for this project")
    if not shutil.which("git"):
        raise HTTPException(500, detail="git not available")
    r = subprocess.run(
        ["git", "-C", str(pdir), "log", "--pretty=format:%H%x09%ai%x09%s", "-n", "50"],
        capture_output=True, text=True,
    )
    runs = []
    for line in (r.stdout or "").strip().split("\n"):
        if not line:
            continue
        sha, ai_ts, subject = (line.split("\t", 2) + ["", ""])[:3]
        runs.append({"sha": sha, "short": sha[:8], "ts": ai_ts, "subject": subject})
    return TEMPLATES.TemplateResponse(request, "project_runs.html", {
        "project": get_project_meta(project_key) or {"project_key": project_key},
        "runs": runs,
    })


@app.get("/p/{project_key}/diff/{sha}", response_class=HTMLResponse)
def project_diff(request: Request, project_key: str, sha: str):
    pdir = _project_git_dir(project_key)
    if pdir is None:
        raise HTTPException(404, detail="no run history for this project")
    if not shutil.which("git"):
        raise HTTPException(500, detail="git not available")

    # Commit metadata (subject + body)
    r = subprocess.run(
        ["git", "-C", str(pdir), "log", "-1", "--pretty=format:%H%n%ai%n%s%n--BODY--%n%b", sha],
        capture_output=True, text=True,
    )
    if r.returncode != 0 or not r.stdout.strip():
        raise HTTPException(404, detail=f"commit {sha} not found")
    parts = r.stdout.split("\n--BODY--\n", 1)
    head = parts[0].split("\n", 2)
    sha_full = head[0]
    ai_ts = head[1] if len(head) > 1 else ""
    subject = head[2] if len(head) > 2 else ""
    body = parts[1] if len(parts) > 1 else ""

    # Raw diff — let diff2html (client-side) render it as side-by-side or unified.
    r = subprocess.run(
        ["git", "-C", str(pdir), "show", "--pretty=", "--no-color", sha_full],
        capture_output=True, text=True,
    )
    diff_text = r.stdout or ""

    # Neighbors for prev navigation; absence indicates this is the initial commit.
    r_prev = subprocess.run(
        ["git", "-C", str(pdir), "rev-parse", f"{sha_full}^"],
        capture_output=True, text=True,
    )
    prev_sha = r_prev.stdout.strip() if r_prev.returncode == 0 else None
    is_initial = prev_sha is None

    return TEMPLATES.TemplateResponse(request, "diff.html", {
        "project": get_project_meta(project_key) or {"project_key": project_key},
        "commit": {
            "sha": sha_full,
            "short": sha_full[:8],
            "ts": ai_ts,
            "subject": subject,
            "body": body,
        },
        "diff_text": diff_text,
        "prev_sha": prev_sha,
        "is_initial": is_initial,
    })


def serve(host: str | None = None, port: int | None = None):
    import uvicorn

    from watchmen import config
    host = host or config.VIEWER_DEFAULT_HOST
    port = port if port is not None else config.viewer_port()
    print(f"\n  watchmen viewer running at http://{host}:{port}\n", flush=True)
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    serve()
