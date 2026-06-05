"""watchmen insights — cross-repo deep digest.

Largest single command extracted from cli.py during Phase 3. Aggregates
curator output, corpus signals, and adapter mix across every tracked
project into one terminal view, optionally pairing the static aggregation
with a two-stage LLM synthesis (per-repo → cross-repo) cached at
~/.watchmen/insights/<timestamp>.md.

Pairs with Anthropic's `/insights` slash command: that one narrates a
single corpus globally; this one is multi-adapter, multi-repo, and
persistent (each run is git-friendly markdown saved on disk).
"""

from __future__ import annotations

import json
import random
from datetime import datetime
from pathlib import Path

from watchmen import state
from watchmen import config as _config


def _default_model() -> str:
    """Per-invocation default model — resolves through the active provider."""
    return _config.default_model()


# Module-level alias for any caller still importing `insights.DEFAULT_MODEL`.
# Resolved at import time against the current active provider.
DEFAULT_MODEL = _default_model()
# Helpers moved out of cli.py during the earlier Phase 3 slices. Aliased
# under the `_name` convention so the verbatim cmd_insights body below
# doesn't churn — the entire function block + 13 helpers were extracted
# without rewriting call sites.
from watchmen.ui import (
    dim as _dim,
    sparkline as _sparkline,
)
from watchmen.util import (
    ADAPTER_SHORT as _ADAPTER_SHORT,
    adapter_breakdown as _adapter_breakdown,
    analyses_base as _analyses_base,
    bundle_base as _bundle_base,
    corpus_db_path as _corpus_db_path,
    repo_friction_signals as _repo_friction_signals,
)


# ─── Insights closing taglines — Watchmen canon nihilism ────────────────────
# One-liner printed at the very end of `watchmen insights`. Random rotation
# from a small pool so the digest never feels canned. Lines kept in-character
# (Manhattan / Rorschach / Veidt voices) without being on-the-nose.

_WATCHMEN_INSIGHTS_TAGLINES = (
    "Nothing ever ends.",
    "Quis custodiet ipsos custodes?",
    "Tick. Tock.",
    "On Mars, this would already be inevitable.",
    "Every record of this exists somewhere in time.",
    "I see what is to come. You see what is.",
    "The world is the way it is. I cannot reduce its complexity for you.",
    "We're all puppets. I can just see the strings.",
    "I leave it for you to mend.",
    "No one is watching us watch.",
)


def cmd_insights(args) -> int:
    """Cross-repo digest. Aggregates curator output, corpus, and adapter
    signals across every tracked project into one terminal view.

    Pairs with Anthropic's `/insights` slash command (v2.1.117+): that one
    produces an LLM-narrated single-corpus report (Claude Code transcripts
    only). `watchmen insights` produces a structured cross-repo +
    multi-adapter digest grounded in actual curated artifacts — no LLM
    call, fully local."""
    import sqlite3
    from watchmen import metrics as _metrics
    from rich.console import Console
    from rich.table import Table

    console = Console()
    # `--list` short-circuits: just show what's already saved on disk.
    if getattr(args, "list_digests", False):
        _list_saved_digests(console)
        return 0

    state.init_db()
    projects = state.list_projects()
    base = _bundle_base()

    if not projects:
        print(_dim("No projects tracked yet — run `watchmen init` to add one."))
        return 1

    # Adapter totals across the whole corpus — drives the header line and
    # also the "untapped corpora" friction signal below. corpus.db is the
    # source of truth; if it's missing we skip the section gracefully.
    corpus_db = _corpus_db_path()
    adapter_totals: dict[str, int] = {}
    if corpus_db.exists():
        cc = sqlite3.connect(corpus_db)
        rows = cc.execute(
            """SELECT agent, COUNT(*) AS n FROM sessions
               WHERE is_subagent = 0 GROUP BY agent ORDER BY n DESC"""
        ).fetchall()
        cc.close()
        for agent, n in rows:
            adapter_totals[agent] = n
    total_sessions = sum(adapter_totals.values())

    # Coverage: how many tracked projects have a non-empty skills/ dir.
    curated_count = 0
    for p in projects:
        skills_dir = base / p["project_key"] / "skills"
        if skills_dir.exists() and any(d.is_dir() for d in skills_dir.iterdir()):
            curated_count += 1

    # Banner — `◷` watch glyph + wordmark + a dim rule. The rule is plain
    # text inside Rich markup (not _dim() raw ANSI) so Rich doesn't try to
    # re-parse the escape sequences as markup. That was the bug behind the
    # literal `[90m...[0m` text in earlier runs.
    from datetime import datetime as _dt
    now_str = _dt.now().strftime("%Y-%m-%d %H:%M")
    console.print()
    console.print(f"[bold]◷ Watchmen[/]  [dim]global digest · {now_str}[/]")
    console.print(f"[dim]{'─' * 65}[/]")
    adapter_str = " · ".join(
        f"[cyan]{n}[/] {_ADAPTER_SHORT.get(a, a)}"
        for a, n in sorted(adapter_totals.items(), key=lambda x: -x[1])
    ) or "[dim]no sessions in corpus.db yet[/]"
    console.print(
        f"  sessions      {adapter_str}   "
        f"[dim]({total_sessions} total · {len(projects)} repos tracked)[/]"
    )
    console.print(
        f"  curated       [cyan]{curated_count}[/] / [cyan]{len(projects)}[/] "
        "repos have skill bundles"
    )

    # Per-repo table. Sorted: curated repos first (by skill count), then
    # uncurated by raw session volume. Eye should land on what's been
    # delivered before what's still waiting.
    def _row_data(p: dict) -> dict:
        key = p["project_key"]
        proj_dir = base / key
        skills_dir = proj_dir / "skills"
        skills_n = (
            sum(1 for d in skills_dir.iterdir() if d.is_dir())
            if skills_dir.exists() else 0
        )
        # 14-day window so the sparkline fits exactly in the table's
        # width=14 column without truncating with an ellipsis (the 30-day
        # version was rendering as e.g. `▁▁█▁▁██▁▁██▁…` — losing the most
        # recent half of the data, which is what the user actually cares
        # about). Two weeks is also the right horizon for "what's hot now".
        daily = _metrics.daily_metrics(key, days=14) or []
        # daily_metrics returns most-recent-first; reverse for time-order
        sess_values = [float(r.get("sessions", 0)) for r in reversed(daily)]
        adapter = _adapter_breakdown(key)
        runs = state.recent_runs(limit=1, project_key=key)
        last_run = "—"
        if runs:
            last_run = (runs[0]["ended_at"] or runs[0]["started_at"] or "—")[:10]
        try:
            prog = state.get_project_progress(key)
            unanalyzed = prog.get("new_prompts_since_last_analysis", 0) or 0
        except Exception:
            unanalyzed = 0
        # Friction signals from corpus.db — tool-error totals and the
        # number of prompts matching frustration markers. Both feed the
        # LLM digest and the static "Signals" row below. These are the
        # signals that `/insights` surfaces as charts but watchmen was
        # blind to until now.
        tool_errors, top_error_tools, frust_count, frust_samples = (
            _repo_friction_signals(key)
        )
        return {
            "key": key, "skills_n": skills_n, "sess_values": sess_values,
            "adapter": adapter, "last_run": last_run, "unanalyzed": unanalyzed,
            "total_sess": sum(adapter.values()),
            "tool_errors": tool_errors,
            "top_error_tools": top_error_tools,
            "frust_count": frust_count,
            "frust_samples": frust_samples,
        }

    rows = [_row_data(p) for p in projects]
    rows.sort(key=lambda r: (-r["skills_n"], -r["total_sess"]))

    repo_tbl = Table(
        title="\nRepos", show_header=True, header_style="bold magenta",
        box=None, padding=(0, 1, 0, 1),
    )
    repo_tbl.add_column("project", style="bold", no_wrap=True)
    repo_tbl.add_column("skills", justify="right")
    repo_tbl.add_column("activity (14d)", width=14, no_wrap=True)
    repo_tbl.add_column("adapters", no_wrap=True)
    repo_tbl.add_column("last run", justify="right", no_wrap=True)
    repo_tbl.add_column("pending", justify="right", no_wrap=True)
    for r in rows:
        # Cell values pass through Rich's markup parser, so all dim/cyan/
        # yellow styling uses [tag]…[/] markup rather than _dim()-style
        # raw ANSI. Raw ANSI would render literally because Rich sees
        # `[90m` as text containing unmatched-bracket markup.
        spark = _sparkline(r["sess_values"]) if r["sess_values"] else "[dim]" + (" " * 8) + "[/]"
        # Compact adapter format for this table — `cc:4 cd:3` style instead
        # of the padded `   4 cc ·    3 cd · …` so the row fits a 100-col
        # terminal. Skips adapters with zero sessions to reduce noise.
        adapter_parts = [
            f"{_ADAPTER_SHORT.get(a, a)}:{r['adapter'].get(a, 0)}"
            for a in ("claude_code", "codex", "pi", "cursor") if r["adapter"].get(a, 0) > 0
        ]
        adapter_short = " ".join(adapter_parts) or "[dim]—[/]"
        skills_str = f"[cyan]{r['skills_n']}[/]" if r["skills_n"] else "[dim]0[/]"
        unanalyzed_str = (
            f"[yellow]{r['unanalyzed']}[/]" if r["unanalyzed"] >= 30
            else (str(r["unanalyzed"]) if r["unanalyzed"] else "[dim]0[/]")
        )
        repo_tbl.add_row(
            r["key"], skills_str, spark, adapter_short, r["last_run"], unanalyzed_str,
        )
    console.print(repo_tbl)

    # Cross-repo pattern detection. Same slug surfacing as a candidate in
    # ≥2 repos is the signal — either it's a coding habit that spans repos
    # (worth a shared skill upstream) or one repo curated it and another
    # didn't (worth promoting). We tag each hit as ✓ curated or · candidate
    # so the user can see at a glance which direction to push.
    pattern_idx: dict[str, list[tuple[str, str]]] = {}
    for p in projects:
        key = p["project_key"]
        proj_dir = base / key
        cand_path = proj_dir / "_candidates.json"
        if not cand_path.exists():
            continue
        try:
            cands = json.loads(cand_path.read_text())
        except Exception:
            continue
        skills_dir = proj_dir / "skills"
        existing = (
            {d.name for d in skills_dir.iterdir() if d.is_dir()}
            if skills_dir.exists() else set()
        )
        for c in cands:
            slug = c.get("slug")
            if not slug:
                continue
            status = "curated" if slug in existing else "candidate"
            pattern_idx.setdefault(slug, []).append((key, status))
    cross = [(slug, hits) for slug, hits in pattern_idx.items() if len(hits) >= 2]
    cross.sort(key=lambda x: (-len(x[1]), x[0]))
    if cross:
        console.print(
            "\n[bold]◆ Cross-repo patterns[/]  "
            "[dim](slugs surfaced in ≥2 repos)[/]"
        )
        for slug, hits in cross[:8]:
            parts = []
            for key, status in hits:
                glyph = "[green]✓[/]" if status == "curated" else "[yellow]·[/]"
                parts.append(f"{glyph} {key}")
            console.print(f"  [bold]{slug}[/]  " + " · ".join(parts))

    # Friction signal v1: tracked repos with captured sessions but no
    # curation yet. Lists biggest-corpus-first because that's where the
    # next curate run produces the most leverage.
    zero_curate = []
    for r in rows:
        if r["skills_n"] == 0 and r["total_sess"] > 0:
            zero_curate.append((r["key"], r["total_sess"]))
    zero_curate.sort(key=lambda x: -x[1])
    if zero_curate:
        console.print(
            "\n[bold]◆ Untapped corpora[/]  "
            "[dim](sessions captured, no skills curated yet)[/]"
        )
        for key, sess in zero_curate[:5]:
            console.print(
                f"  [yellow]·[/] {key}  [dim]{sess} sessions[/]"
            )

    # Friction signals from corpus.db — surfaces tool-error hot-spots and
    # frustration-marker prompt counts per repo. Headline-only here; the
    # full per-repo facts (including sample quotes) go to the LLM.
    total_errors = sum(r["tool_errors"] for r in rows)
    total_frust = sum(r["frust_count"] for r in rows)
    if total_errors or total_frust:
        # Top 3 repos by tool errors + top 3 by frustration — give the
        # user a one-glance read on which repos are hot.
        err_top = sorted(rows, key=lambda r: -r["tool_errors"])[:3]
        err_top = [r for r in err_top if r["tool_errors"] > 0]
        frust_top = sorted(rows, key=lambda r: -r["frust_count"])[:3]
        frust_top = [r for r in frust_top if r["frust_count"] > 0]
        console.print("\n[bold]◆ Friction signals[/]  [dim](corpus.db)[/]")
        if err_top:
            err_str = " · ".join(f"{r['key']}({r['tool_errors']})" for r in err_top)
            console.print(
                f"  [red]✗[/] {total_errors} tool errors total  "
                f"[dim]top: {err_str}[/]"
            )
        if frust_top:
            frust_str = " · ".join(f"{r['key']}({r['frust_count']})" for r in frust_top)
            console.print(
                f"  [yellow]![/] {total_frust} frustration markers  "
                f"[dim](no wait / bruh / :( / wtf / …) top: {frust_str}[/]"
            )

    # Deep digest — saved to ~/.watchmen/insights/. Two-stage pipeline
    # (Stage 1 per-repo synthesis in parallel + Stage 2 cross-repo
    # aggregation). Each run costs ~$0.05-0.10 and takes 30-60s, so we
    # cache the markdown output and prompt view-vs-regenerate when one
    # exists. --no-llm skips the digest entirely; --regenerate / --view
    # / --list bypass the prompt for scripting.
    if getattr(args, "no_llm", False):
        console.print()
        return 0

    model = getattr(args, "model", DEFAULT_MODEL)
    latest = _latest_digest_path()
    action = _decide_digest_action(args, latest, console)
    if action == "quit":
        console.print()
        return 0
    if action == "view":
        _render_saved_digest(console, latest)
        console.print()
        return 0
    # action == "regenerate"
    with console.status(
        f"[dim]running deep digest pipeline · {model} · ~30-60s …[/]",
        spinner="dots",
    ):
        digest = _insights_pipeline(rows, cross, zero_curate, adapter_totals, model)
    if digest:
        from rich.markdown import Markdown
        repos_synthesized = sum(
            1 for r in rows
            if (_analyses_base() / r["key"] / "_running.md").exists()
        )
        saved = _save_digest(digest, model, repos_synthesized)
        console.print(f"\n[bold]◉ Deep digest[/]  [dim]({model})[/]")
        console.print(Markdown(digest))
        console.print(f"\n  [dim]saved → {saved}[/]")

        # Cross-agent comparison — separate LLM call, cached under a stable
        # filename so the viewer can find it without scanning timestamped
        # digest files. Skipped automatically when <2 adapters have enough
        # activity to compare (the LLM helper returns None there).
        from watchmen import metrics as _metrics
        with console.status(
            "[dim]synthesizing cross-agent comparison · ~10-20s …[/]",
            spinner="dots",
        ):
            facts = _metrics.agent_comparison_facts(days=90)
            narrative = _cross_agent_narrative(facts, model)
        if narrative:
            cmp_path = _save_cross_agent_narrative(narrative, model)
            console.print(f"\n[bold]◉ How you use each agent[/]  [dim]({model})[/]")
            console.print(Markdown(narrative))
            console.print(f"\n  [dim]saved → {cmp_path}[/]")
    _print_watchmen_tagline(console)
    console.print()
    return 0


def _print_watchmen_tagline(console) -> None:
    """One-line random nihilist closer. Rotates from _WATCHMEN_INSIGHTS_TAGLINES
    so back-to-back runs don't feel canned. Dim italic so it reads as a
    margin note, not as content."""
    tag = random.choice(_WATCHMEN_INSIGHTS_TAGLINES)
    console.print(f"\n  [dim italic]— {tag}[/]")


# ─── Insights digest cache (~/.watchmen/insights/) ────────────────────────


def _insights_cache_dir() -> Path:
    d = Path.home() / ".watchmen" / "insights"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _latest_digest_path() -> Path | None:
    """Newest timestamped digest in the insights cache, or None if empty.

    Filters by `[0-9]*.md` so stable-named files (e.g. agent_comparison.md)
    that share the cache directory aren't mistaken for digest runs — `sorted(...,
    reverse=True)` on `*.md` would bubble alphabetical names above date-
    prefixed ones and silently return the wrong file."""
    cache = _insights_cache_dir()
    digests = sorted(cache.glob("[0-9]*.md"), reverse=True)
    return digests[0] if digests else None


def _save_digest(content: str, model: str, repos_synthesized: int) -> Path:
    """Persist a digest run with YAML frontmatter for metadata. The
    frontmatter lets `watchmen insights` show the saved run's age + model
    in the view-vs-regenerate prompt without parsing a sidecar JSON."""
    ts = datetime.now()
    fname = ts.strftime("%Y-%m-%d_%H-%M-%S") + ".md"
    path = _insights_cache_dir() / fname
    body = (
        "---\n"
        f"generated_at: {ts.isoformat(timespec='seconds')}\n"
        f"model: {model}\n"
        f"repos_synthesized: {repos_synthesized}\n"
        "---\n\n"
        + content
    )
    path.write_text(body)
    return path


def _read_digest_metadata(path: Path) -> tuple[dict, str]:
    """Split frontmatter (if any) from body. Returns (metadata_dict, body).
    Frontmatter parsing is intentionally minimal (one-line `key: value`
    pairs) — no PyYAML dep just for this."""
    text = path.read_text()
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, text
    meta_block = text[4:end]
    body = text[end + 5:].lstrip("\n")
    meta: dict[str, str] = {}
    for line in meta_block.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip()
    return meta, body


def _decide_digest_action(args, latest: Path | None, console) -> str:
    """Resolve view/regenerate/quit from flags + cache state + tty.

    Precedence: --regenerate / --view flags > prompt (when interactive +
    cache exists) > regenerate (no cache) > view (cache exists but stdin
    not a tty, so we can't prompt and refuse to silently burn API credit)."""
    import sys as _sys
    if getattr(args, "regenerate", False):
        return "regenerate"
    if getattr(args, "view", False):
        if latest is None:
            console.print("[yellow]no saved digest yet — run without --view first.[/]")
            return "quit"
        return "view"
    if latest is None:
        # First-ever run — go straight to regenerate, no prompt needed.
        return "regenerate"
    if not _sys.stdin.isatty():
        # Non-interactive context (piped, CI). Refuse to silently spend
        # API credit; default to the safe action.
        console.print("[dim]  \\[non-tty: defaulting to view; pass --regenerate to force a fresh run][/]")
        return "view"
    return _prompt_digest_action(latest, console)


def _prompt_digest_action(latest: Path, console) -> str:
    """Interactive prompt shown when a cached digest exists. Two options
    plus quit: (v)iew the saved digest or (r)egenerate a fresh one."""
    from rich.prompt import Prompt
    meta, _ = _read_digest_metadata(latest)
    ts = meta.get("generated_at", "?")
    model = meta.get("model", "?")
    repos = meta.get("repos_synthesized", "?")
    # Age in a human-readable form so the user can decide if it's stale.
    age_str = ""
    try:
        gen = datetime.fromisoformat(ts)
        delta = datetime.now() - gen
        hours = int(delta.total_seconds() // 3600)
        if hours < 1:
            age_str = f" ({int(delta.total_seconds() // 60)}m ago)"
        elif hours < 48:
            age_str = f" ({hours}h ago)"
        else:
            age_str = f" ({hours // 24}d ago)"
    except Exception:
        pass
    console.print(
        f"\n[bold]Last digest:[/] {ts}[dim]{age_str}[/]  "
        f"[dim]·[/] {model}  [dim]·[/] {repos} repos synthesized"
    )
    choice = Prompt.ask(
        "  [(v)iew · (r)egenerate · (q)uit]",
        choices=["v", "r", "q"],
        default="v",
        show_choices=False,
    ).lower()
    return {"v": "view", "r": "regenerate", "q": "quit"}[choice]


def _render_saved_digest(console, path: Path) -> None:
    """Render a previously-saved digest with a header line showing when
    + what model produced it. Frontmatter is stripped before rendering
    so the markdown reads cleanly."""
    from rich.markdown import Markdown
    meta, body = _read_digest_metadata(path)
    ts = meta.get("generated_at", "?")
    model = meta.get("model", "?")
    console.print(
        f"\n[bold]◉ Deep digest[/]  "
        f"[dim]({model} · generated {ts})[/]"
    )
    console.print(Markdown(body))
    console.print(f"\n  [dim]source → {path}[/]")
    _print_watchmen_tagline(console)


def _list_saved_digests(console) -> None:
    """`watchmen insights --list`: show every saved digest with date,
    model, and repos-synthesized count. Helps users figure out what
    they've already paid for before regenerating."""
    cache = _insights_cache_dir()
    digests = sorted(cache.glob("*.md"), reverse=True)
    if not digests:
        console.print("[dim]\nNo saved digests yet. Run `watchmen insights` to create one.\n[/]")
        return
    console.print(f"\n[bold]Saved digests ({len(digests)})[/]  [dim]{cache}[/]\n")
    for p in digests:
        meta, _ = _read_digest_metadata(p)
        ts = meta.get("generated_at", p.stem)
        model = meta.get("model", "?")
        repos = meta.get("repos_synthesized", "?")
        size_kb = p.stat().st_size / 1024
        console.print(
            f"  [bold]{ts}[/]  [dim]·[/] {model}  "
            f"[dim]·[/] {repos} repos  [dim]·[/] {size_kb:.1f}kb  "
            f"[dim]·[/] {p.name}"
        )
    console.print()


def _insights_pipeline(
    rows: list[dict],
    cross: list[tuple[str, list[tuple[str, str]]]],
    zero_curate: list[tuple[str, int]],
    adapter_totals: dict[str, int],
    model: str,
) -> str | None:
    """Two-stage deep digest. Stage 1 reads each repo's longitudinal
    thesis + curator log + candidates list and produces a structured
    per-repo summary (parallel). Stage 2 cross-synthesizes those plus
    the static facts into a markdown digest with themes / friction /
    skill gaps / underused capabilities / next moves."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    # Only repos with a thesis on disk are worth Stage 1 — without it
    # the LLM has no longitudinal context to summarize. (Uncurated /
    # un-analyzed repos still appear in Stage 2 via static facts.)
    eligible = []
    for r in rows:
        thesis_path = _analyses_base() / r["key"] / "_running.md"
        if thesis_path.exists():
            eligible.append(r)
    if not eligible and not rows:
        return None

    per_repo: dict[str, str] = {}
    if eligible:
        with ThreadPoolExecutor(max_workers=min(4, len(eligible))) as pool:
            futures = {pool.submit(_repo_synthesis, r, model): r["key"] for r in eligible}
            for fut in as_completed(futures):
                key = futures[fut]
                try:
                    out = fut.result()
                except Exception:
                    out = None
                if out:
                    per_repo[key] = out

    return _cross_repo_synthesis(per_repo, rows, cross, zero_curate, adapter_totals, model)


def _repo_synthesis(repo_row: dict, model: str) -> str | None:
    """Stage 1: synthesize one repo's thesis + curator log + candidates
    into a structured summary. Truncates inputs to fit a single-call
    budget; the value is in citing actual dates/slugs/patterns, not
    in exhaustive coverage."""
    key = repo_row["key"]
    proj_dir = _bundle_base() / key
    thesis_path = _analyses_base() / key / "_running.md"
    log_path = proj_dir / "_curation_log.md"
    cand_path = proj_dir / "_candidates.json"

    thesis = thesis_path.read_text() if thesis_path.exists() else ""
    curation_log = log_path.read_text() if log_path.exists() else ""
    candidates = cand_path.read_text() if cand_path.exists() else "[]"
    # Truncate to fit a ~12k char input budget per call (~3k tokens).
    # Thesis is the densest signal — give it the most room.
    thesis = thesis[:10000]
    curation_log = curation_log[:6000]
    candidates = candidates[:4000]

    system = (
        "You are a senior engineer analyzing one developer's usage of a coding "
        "agent on a single repository. You receive: (1) a longitudinal usage "
        "thesis compiled by an analyst agent, (2) the curator's decision log "
        "for skill bundles, (3) the candidates list (proposed slugs).\n\n"
        "Produce a structured per-repo summary capturing:\n"
        "- **Themes**: 3-5 bullets on what kind of work happens here, with "
        "specific dates / PRs / files from the thesis.\n"
        "- **Friction**: 2-4 recurring problems with concrete examples cited.\n"
        "- **User signals**: 2-3 communication/trust/delegation patterns "
        "specific to this developer in this repo.\n"
        "- **Skill gaps**: 1-3 patterns visible in the thesis that ARE NOT "
        "in the curated bundle (cross-reference candidates vs curated).\n\n"
        "Cite evidence inline — dates, slugs, file paths, exact phrasings. "
        "No generic advice. 250-400 words. Markdown bullets only, no prose."
    )
    user = (
        f"## Repo: {key}\n"
        f"Curated skills: {repo_row['skills_n']} · "
        f"Total sessions: {repo_row['total_sess']} · "
        f"Pending: {repo_row['unanalyzed']}\n\n"
        f"### Thesis\n{thesis}\n\n"
        f"### Curation log\n{curation_log}\n\n"
        f"### Candidates\n{candidates}"
    )
    return _one_shot_llm(system, user, model, max_tokens=900)


def _cross_repo_synthesis(
    per_repo: dict[str, str],
    rows: list[dict],
    cross: list[tuple[str, list[tuple[str, str]]]],
    zero_curate: list[tuple[str, int]],
    adapter_totals: dict[str, int],
    model: str,
) -> str | None:
    """Stage 2: aggregate Stage 1 outputs + static facts into the final
    cross-repo digest. This is where themes-across-work, propagatable
    skill gaps, and CLAUDE.md / hooks / MCP suggestions get surfaced."""
    facts = _insights_facts(adapter_totals, rows, cross, zero_curate)
    if per_repo:
        summaries = "\n\n".join(
            f"## {k}\n{v}" for k, v in per_repo.items()
        )
    else:
        summaries = "(no curated repos had a thesis on disk to synthesize)"
    system = (
        "You are a senior engineer producing a cross-repo digest of how a "
        "developer uses coding agents (Claude Code, Codex, pi.dev) across "
        "multiple projects. You receive: (1) static aggregate facts including "
        "per-repo tool-error counts and frustration-marker prompt samples, "
        "(2) per-repo deep synthesis from a prior stage.\n\n"
        "Produce a markdown digest with exactly these 6 sections, in order:\n\n"
        "### 🔭 At a glance\n"
        "3-4 punchy sentences that lead the digest. State: (a) the single "
        "most important recurring pattern across repos, (b) the biggest "
        "untapped opportunity, (c) the highest-leverage next action. Specific "
        "and grounded — name a repo / slug / count. No hedging. This is the "
        "first thing the user reads.\n\n"
        "### Themes across your work\n"
        "3-5 cross-cutting themes citing specific repos. What recurs?\n\n"
        "### Friction patterns\n"
        "2-4 recurring problems that span repos. Use the tool-error counts "
        "and frustration-marker samples in the facts — quote at least one "
        "real frustration prompt verbatim if available.\n\n"
        "### Skill gaps\n"
        "Patterns curated in one repo that should propagate to others, OR "
        "patterns visible in theses that aren't curated anywhere. Cite slugs.\n\n"
        "### Underused capabilities\n"
        "Concrete CLAUDE.md rules / hooks / MCP servers / subagent patterns "
        "that the data shows would help. Justify with evidence — tool-error "
        "spikes, frustration counts, or specific friction examples. No "
        "generic agent-coding advice.\n\n"
        "### Concrete next moves\n"
        "3-5 specific actions — copy-pastable commands or filename targets. "
        "No 'consider' or 'maybe' — name the exact slug/repo/file.\n\n"
        "Total 600-1000 words. Every bullet grounded in cited evidence."
    )
    user = (
        f"# Static facts\n{facts}\n\n"
        f"# Per-repo deep synthesis\n{summaries}"
    )
    return _one_shot_llm(system, user, model, max_tokens=3000)


def _cross_agent_narrative(facts: dict, model: str) -> str | None:
    """LLM-synthesized prose comparing how the user works across coding
    agents. Fed the structured per-adapter facts from
    `metrics.agent_comparison_facts`; returns markdown with one paragraph
    per adapter followed by a "what each agent is best at" closer.

    Returns None when there's nothing useful to compare — fewer than two
    adapters with meaningful activity (≥10 prompts each) means the
    narrative would just describe one tool in isolation, which the
    profile card already does."""
    adapters = facts.get("adapters") or []
    eligible = [a for a in adapters if (a.get("prompts") or 0) >= 10]
    if len(eligible) < 2:
        return None

    # Compact JSON-ish summary the LLM can scan in one breath. Keep numbers
    # concrete (not "Codex is cheaper" — give the actual $/prompt) so the
    # model is grounded in the facts rather than free-associating.
    blocks = []
    for a in eligible:
        top_tools = ", ".join(f"{n} ({c})" for n, c in (a.get("top_tools") or [])[:5])
        top_projects = ", ".join(
            _project_label(p) + f" ({c})" for p, c in (a.get("top_projects") or [])[:3]
        )
        blocks.append(
            f"## {a['label']} ({a['agent']})\n"
            f"- sessions: {a['sessions']}, active days: {a['active_days']}, projects: {a['projects']}\n"
            f"- prompts: {a['prompts']}, tool calls: {a['tool_calls']}, "
            f"tool error rate: {a['tool_error_rate']:.2%}\n"
            f"- $/prompt: ${a['cost_per_prompt']:.4f}, $/session: ${a['cost_per_session']:.2f}, "
            f"total cost: ${a['cost_usd']:.2f}\n"
            f"- prompts/session: {a['prompts_per_session']:.1f}, "
            f"tool calls/session: {a['tool_calls_per_session']:.1f}, "
            f"avg session duration: {a['avg_session_seconds']/60:.0f}min, "
            f"cache hit rate: {a['cache_hit_rate']:.0%}\n"
            f"- dominant model: {a.get('dominant_model') or '(unknown)'}\n"
            f"- top tools: {top_tools or '(none)'}\n"
            f"- most-used projects: {top_projects or '(none)'}\n"
            f"- skill suggestions fired (plugin): {a.get('suggestions_fired', 0)}\n"
        )

    system = (
        "You are a senior engineer comparing how a single developer uses two "
        "or more coding-agent CLIs (e.g. Claude Code, OpenAI Codex, pi.dev) "
        "based on N days of session data. The user wants to understand which "
        "agent they reach for which kind of work, what each is best at in "
        "their hands, and where one is being under- or over-used.\n\n"
        "Produce a markdown section titled '### How you use each agent' with "
        "exactly three subsections:\n\n"
        "**Per-agent character** — one short paragraph per agent (~3-4 sentences). "
        "Cite the specific numbers: $/prompt, prompts-per-session, top tools, "
        "dominant project. Infer character from the mix: e.g. 'shell-heavy + "
        "short sessions = quick targeted execution', 'edit/read/bash-balanced + "
        "long sessions = sustained build work'. No generic AI-coding platitudes.\n\n"
        "**Where each one wins** — 2-4 bullets identifying which agent the "
        "data shows is best suited for which kind of task. Ground each "
        "claim in the comparison: cite a specific cost or tool-mix gap. If "
        "the data doesn't support a strong claim, say so explicitly.\n\n"
        "**Re-balancing suggestion** — one or two bullets. Concrete: a kind "
        "of task the user is doing in one agent that the other's data "
        "suggests would be cheaper, faster, or less error-prone. Skip this "
        "section entirely if the comparison is too even to suggest a move.\n\n"
        "Total 250-400 words. Every claim must reference at least one "
        "number from the facts. Never invent metrics that aren't in the data."
    )
    user_msg = (
        f"# Cross-agent comparison facts — last {facts.get('window_days', 90)} days "
        f"(since {facts.get('since', '?')})\n\n" + "\n".join(blocks)
    )
    return _one_shot_llm(system, user_msg, model, max_tokens=900)


def _project_label(project_dir: str) -> str:
    """Translate an absolute project_dir to its tracked label (e.g.
    /Users/foo/dev/kai → "kai"). Falls back to the last path segment when
    the project isn't tracked, so the narrative never prints absolute
    paths that don't generalize across machines."""
    try:
        from watchmen.util import adapter_breakdown  # noqa: F401 — only here to confirm import works
        idx = Path.home() / ".watchmen" / "projects.json"
        if idx.exists():
            projects = json.loads(idx.read_text())
            for p in projects:
                if p.get("source_repo") and Path(p["source_repo"]).resolve() == Path(project_dir).resolve():
                    return p.get("project_key") or Path(project_dir).name
    except Exception:
        pass
    return Path(project_dir).name


def _save_cross_agent_narrative(content: str, model: str) -> Path:
    """Persist the cross-agent narrative under a stable filename so the
    viewer can find it without globbing. Overwritten on each digest run.
    Includes YAML frontmatter for generated_at + model so the viewer can
    show 'last refreshed N days ago'."""
    path = _insights_cache_dir() / "agent_comparison.md"
    ts = datetime.now()
    body = (
        "---\n"
        f"generated_at: {ts.isoformat(timespec='seconds')}\n"
        f"model: {model}\n"
        "---\n\n"
        + content
    )
    path.write_text(body)
    return path


def _latest_cross_agent_narrative() -> tuple[dict, str] | None:
    """Read the saved cross-agent narrative, returning (frontmatter, body)
    or None if not yet generated. Used by the viewer's /metrics + /insights
    routes to embed the narrative without depending on the digest pipeline."""
    path = _insights_cache_dir() / "agent_comparison.md"
    if not path.exists():
        return None
    return _read_digest_metadata(path)


def _one_shot_llm(
    system: str, user: str, model: str, max_tokens: int = 500,
) -> str | None:
    """Single OpenRouter chat call. Returns response text or None on any
    error (missing key, network, malformed response). Never raises so
    callers can run inside a parallel dispatcher without aborting peers."""
    try:
        from watchmen import agent as _ag
        import httpx
    except Exception:
        return None
    try:
        with httpx.Client(timeout=180.0) as client:
            data = _ag.chat_call(
                client,
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                model=model,
                agent_name="insights",
                max_retries=2,
                temperature=0.3,
                max_tokens=max_tokens,
            )
            content = data["choices"][0]["message"]["content"]
            return content.strip() if content else None
    except Exception:
        return None


def _insights_facts(
    adapter_totals: dict[str, int],
    rows: list[dict],
    cross: list[tuple[str, list[tuple[str, str]]]],
    zero_curate: list[tuple[str, int]],
) -> str:
    """Package the static aggregation into a structured plain-text prompt
    for the LLM narrator. Stays under ~2k tokens to keep the call cheap."""
    lines: list[str] = []
    lines.append(
        f"Global corpus: {sum(adapter_totals.values())} sessions across "
        f"{len(rows)} tracked repos. Adapters: "
        + ", ".join(f"{n} {_ADAPTER_SHORT.get(a, a)}" for a, n in adapter_totals.items())
    )
    lines.append("")
    lines.append("Per-repo (key | skills | sessions | adapters | pending | last_run | tool_errors | frustration):")
    for r in rows:
        adapter_str = " ".join(
            f"{_ADAPTER_SHORT.get(a, a)}:{r['adapter'].get(a, 0)}"
            for a in ("claude_code", "codex", "pi", "cursor") if r["adapter"].get(a, 0) > 0
        ) or "—"
        lines.append(
            f"- {r['key']} | skills={r['skills_n']} | "
            f"sessions={r['total_sess']} | {adapter_str} | "
            f"pending={r['unanalyzed']} | last_run={r['last_run']} | "
            f"tool_errors={r['tool_errors']} | frustration={r['frust_count']}"
        )
        if r["top_error_tools"]:
            tools_str = ", ".join(f"{t}={n}" for t, n in r["top_error_tools"])
            lines.append(f"    top erroring tools in {r['key']}: {tools_str}")
        if r["frust_samples"]:
            for s in r["frust_samples"]:
                lines.append(f"    frustration sample in {r['key']}: \"{s}\"")
    if cross:
        lines.append("")
        lines.append("Cross-repo candidate slugs (appear in ≥2 repos' _candidates.json):")
        for slug, hits in cross[:8]:
            lines.append(
                f"- {slug}: "
                + ", ".join(f"{k}({s})" for k, s in hits)
            )
    if zero_curate:
        lines.append("")
        lines.append(
            "Untapped repos (captured sessions, zero skills curated): "
            + ", ".join(f"{k}({n})" for k, n in zero_curate)
        )
    return "\n".join(lines)


# ─── Argument parsing ───────────────────────────────────────────────────────
