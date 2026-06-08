"""Pipeline commands — `status`, `ingest`, `analyze`, `curate`, `runs`,
`learn`, `metrics`.

These are the data-flow surface of watchmen: ingest the corpus, run the
analyst, run the curator, inspect the result. Moved out of cli.py during
the Phase 3 split so cli.py is just a dispatcher.

`cmd_learn` orchestrates `cmd_analyze` + `cmd_curate` within this module,
so the cross-call stays intra-module — no callback wiring needed.
"""

import argparse
import subprocess
import sys
from pathlib import Path

from watchmen import config
from watchmen import state
from watchmen.ui import (
    bar as _bar,
    bold as _bold,
    dim as _dim,
    green as _green,
    rich_status as _rich_status,
    sparkline as _sparkline,
    ui_header as _ui_header,
    yellow as _yellow,
)
from watchmen.util import (
    adapter_breakdown as _adapter_breakdown,
    analyses_base as _analyses_base,
    bundle_base as _bundle_base,
    classify_run_failure as _classify_run_failure,
)

# Package root used as cwd for subprocess invocations of `python -m
# watchmen.{corpus,analyze,curate}`. Equivalent to cli.ROOT but computed
# locally to avoid a circular import.
ROOT = Path(__file__).parent.parent


def _print_runtime_state_error(exc: BaseException, *, stderr: bool = True) -> None:
    """Thin shim mirroring cli._print_runtime_state_error so this module
    doesn't need to thread STATE_DB through every call site."""
    from watchmen.paths import STATE_DB
    from watchmen.ui import print_runtime_state_error as _ui_pr
    _ui_pr(STATE_DB, exc, stderr=stderr)


# ─── status ────────────────────────────────────────────────────────────────


def _render_status_overview(console) -> None:
    """Top-of-screen overview: services + provider + corpus health.

    Each section is small (1-3 lines) and falls back gracefully if its data
    source is missing — a fresh install with no corpus / no daemon still
    renders cleanly, just with "—" placeholders.
    """
    import sqlite3
    from datetime import datetime
    from watchmen import paths
    from watchmen.agent import provider_banner

    # Provider banner — matches what analyst/curator runs print at startup,
    # so the user can verify on the status screen which provider their
    # runs are using before they kick one off.
    console.print(f"[dim]{provider_banner()}[/]")
    console.print()

    # Services row — daemon, viewer, hooks. Each shows loaded/not + a tag
    # for the scheduler backend (launchd/systemd/schtasks) so the user knows
    # which OS path the daemon is going through.
    try:
        from watchmen import service, hooks_setup
        daemon_loaded = service.is_daemon_loaded()
        viewer_loaded = service.is_viewer_loaded()
        backend = service.BACKEND_NAME
    except Exception:
        daemon_loaded = viewer_loaded = False
        backend = "?"
    try:
        hooks_state = hooks_setup.is_installed_summary()  # type: ignore[attr-defined]
    except (AttributeError, Exception):
        # `is_installed_summary` is a new helper; older versions just check
        # presence of the settings.json entry. Fall back to a coarse signal.
        hooks_state = None

    def _mark(b: bool) -> str:
        return "[green]✓[/]" if b else "[dim]·[/]"

    parts = [
        f"  {_mark(daemon_loaded)} [bold]daemon[/]  [dim]({backend})[/]",
        f"  {_mark(viewer_loaded)} [bold]viewer[/]  [dim]({backend})[/]",
    ]
    if hooks_state is None:
        parts.append("  [dim]·[/] [bold]hooks[/]   [dim](status unavailable)[/]")
    else:
        # hooks_state is a dict like {"claude_code": True, "codex": False}.
        marks = " ".join(f"{k}={'✓' if v else '·'}" for k, v in hooks_state.items())
        all_on = all(hooks_state.values()) if hooks_state else False
        parts.append(f"  {_mark(all_on)} [bold]hooks[/]   [dim]{marks}[/]")
    console.print("\n".join(parts))

    # Corpus health — last ingest mtime (file_mtime of newest session row),
    # total session count, last-7d Skill calls. Fail-soft on a missing DB.
    corpus_db = paths.WATCHMEN_HOME / "corpus.db"
    if corpus_db.exists():
        try:
            conn = sqlite3.connect(corpus_db)
            conn.row_factory = sqlite3.Row
            session_count = conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"]
            latest_mtime = conn.execute(
                "SELECT MAX(file_mtime) AS m FROM sessions WHERE file_mtime IS NOT NULL"
            ).fetchone()["m"]
            from datetime import timedelta as _td
            now_utc = datetime.utcnow()
            cutoff = (now_utc - _td(days=7)).isoformat()
            skill_7d = conn.execute(
                "SELECT COUNT(*) AS n FROM tool_calls WHERE tool_name='Skill' AND timestamp >= ?",
                (cutoff,),
            ).fetchone()["n"]
            conn.close()
            if latest_mtime:
                latest_str = datetime.fromtimestamp(float(latest_mtime)).strftime("%Y-%m-%d %H:%M")
            else:
                latest_str = "—"
            console.print()
            console.print(
                f"  [dim]corpus:[/]  {session_count:,} sessions · "
                f"latest transcript {latest_str} · "
                f"{skill_7d} skill call(s) · 7d"
            )
        except sqlite3.Error:
            console.print()
            console.print("  [dim]corpus:[/]  [yellow](error reading corpus.db)[/]")
    else:
        console.print()
        console.print(
            "  [dim]corpus:[/]  [yellow]not initialized[/] — run "
            "[cyan]watchmen ingest[/]"
        )
    console.print()


def cmd_status(args) -> int:
    from rich.console import Console
    from rich.table import Table
    console = Console()

    try:
        state.init_db()
    except Exception as e:
        _print_runtime_state_error(e, stderr=False)
        return 1

    # ── Services + provider + corpus ─────────────────────────────────────
    # Pulled up to the top so `watchmen status` answers "is anything
    # broken?" before "what's the project queue?". The four previously
    # separate commands (`daemon status`, `viewer status`, `hooks status`,
    # `doctor`) now feed into one screen.
    _render_status_overview(console)

    tracked = state.list_projects()
    if not tracked:
        _ui_header(console, "status")
        console.print()
        console.print("No projects tracked yet.")
        console.print("[dim]Start with:[/]")
        console.print("  watchmen init")
        console.print("  watchmen ingest")
        console.print("  watchmen list")
        return 0

    rows = []
    needs = 0
    for p in tracked:
        progress = state.get_project_progress(p["project_key"])
        rows.append((p, progress))
        if progress.get("needs_analysis"):
            needs += 1

    enabled = sum(1 for p in tracked if p.get("enabled", 1))
    latest_run = state.recent_runs(limit=1)
    latest = latest_run[0]["started_at"][:19] if latest_run else "never"

    _ui_header(console, "status")
    console.print(
        f"[dim]{len(tracked)} projects[/]  "
        f"[dim]{enabled} enabled[/]  "
        + (f"[yellow]{needs} need analysis[/]" if needs else "[green]all caught up[/]")
        + f"  [dim]latest run: {latest}[/]\n"
    )

    table = Table(show_header=True, header_style="bold", expand=False, box=None, padding=(0, 2, 0, 0))
    table.add_column("project", style="bold")
    table.add_column("state")
    table.add_column("last")
    table.add_column("new", justify="right")
    table.add_column("adapters")
    table.add_column("next")
    for p, progress in rows:
        last_day = p["last_analyst_day"] or "—"
        new_n = progress.get("new_prompts_since_last_analysis", "?")
        st = "enabled" if p["enabled"] else "[yellow]paused[/]"
        if progress.get("needs_analysis"):
            flag = f"watchmen learn {p['project_key']}"
        elif p["last_analyst_day"]:
            flag = "[green]ready[/]"
        else:
            flag = f"watchmen analyze {p['project_key']}"
        bd = _adapter_breakdown(p["project_key"])
        adapters = " ".join(
            f"{label}:{bd.get(agent, 0)}"
            for agent, label in (("claude_code", "cc"), ("codex", "cd"), ("pi", "pi"), ("cursor", "cr"))
            if bd.get(agent, 0)
        ) or "-"
        table.add_row(
            p["project_key"], st, last_day, str(new_n), adapters,
            flag,
        )
    console.print(table)

    runs = state.recent_runs(limit=5)
    if runs:
        console.print()
        console.print("[bold]Recent runs[/]")
        log = Table(show_header=False, box=None, padding=(0, 1, 0, 1))
        log.add_column("when")
        log.add_column("project")
        log.add_column("kind")
        log.add_column("status")
        for r in runs:
            t = (r["started_at"] or "")[:19]
            status = r["status"]
            tag = {"ok": "[green]ok[/]", "running": "[yellow]running[/]"}.get(status, f"[dim]{status}[/]")
            log.add_row(t, r["project_key"], r["kind"], tag)
        console.print(log)
    return 0


# ─── ingest ────────────────────────────────────────────────────────────────


def cmd_ingest(args) -> int:
    cmd = [sys.executable, "-m", "watchmen.corpus", "scan"]
    if getattr(args, "full", False):
        cmd.append("--full")
        print(_dim("Running corpus.py scan (full rebuild)..."))
    else:
        print(_dim("Running corpus.py scan..."))
    r = subprocess.run(cmd, cwd=str(ROOT))
    return r.returncode


# ─── analyze ───────────────────────────────────────────────────────────────


def cmd_analyze(args) -> int:
    state.init_db()
    proj = state.get_project(args.project)
    if not proj and not args.repo:
        print(f"ERROR: project '{args.project}' not tracked. Run `watchmen track {args.project} --repo <path>` first, or pass --repo.")
        return 1

    progress = state.get_project_progress(args.project)
    if progress.get("error"):
        print(f"ERROR: {progress['error']}")
        return 1

    if args.full:
        from_day = None
    else:
        from_day = progress.get("last_analyst_day")
        if from_day and progress["new_prompts_since_last_analysis"] == 0:
            print(_green(f"{args.project}: already up to date (last analyst day: {from_day})"))
            return 0

    cmd = [sys.executable, "-m", "watchmen.analyze", "-p", args.project, "--model", args.model]
    if from_day:
        cmd.extend(["--from-day", from_day])

    run_id = state.start_run(args.project, "analyst", notes=f"from_day={from_day}")
    print(_dim(f"Running: {' '.join(cmd)}"))
    r = subprocess.run(cmd, cwd=str(ROOT))
    if r.returncode == 0:
        analyses_dir = _analyses_base() / args.project
        if analyses_dir.exists():
            day_files = sorted(p.stem for p in analyses_dir.glob("20*.md"))
            if day_files:
                state.update_project(args.project, last_analyst_day=day_files[-1], last_analyst_run=state.now_iso())
        state.finish_run(run_id, "ok")
        print(_green(f"\n{args.project}: analyst run completed."))
    else:
        state.finish_run(run_id, "failed", notes=_classify_run_failure(args.project, r.returncode))
        print(_yellow(f"\n{args.project}: analyst run failed (exit {r.returncode})."))
    return r.returncode


# ─── curate ────────────────────────────────────────────────────────────────


def cmd_curate(args) -> int:
    state.init_db()
    proj = state.get_project(args.project)
    if not proj:
        print(f"ERROR: project '{args.project}' not tracked.")
        return 1

    cmd = [sys.executable, "-m", "watchmen.curate",
           "--project", args.project, "--repo", proj["source_repo"], "--model", args.model]
    if args.regen_claude:
        cmd.extend(["--skip-finder", "--skip-skills"])
    # CLI flags always win; per-project DB settings are the fallback so the
    # user can set "approval_required" once and forget about it.
    if getattr(args, "skip_overlap", False) or proj.get("skip_overlapping_skills"):
        cmd.append("--skip-overlap")
    if getattr(args, "approval_required", False) or proj.get("approval_required"):
        cmd.append("--approval-required")
    if getattr(args, "auto_install", False):
        cmd.append("--auto-install")

    kind = "curator-claude-only" if args.regen_claude else "curator-full"
    run_id = state.start_run(args.project, kind)
    print(_dim(f"Running: {' '.join(cmd)}"))
    r = subprocess.run(cmd, cwd=str(ROOT))
    if r.returncode == 0:
        skills_dir = _bundle_base() / args.project / "skills"
        skill_count = sum(1 for d in skills_dir.iterdir() if d.is_dir()) if skills_dir.exists() else 0
        state.update_project(args.project, last_curator_run=state.now_iso(), last_curator_skill_count=skill_count)
        state.finish_run(run_id, "ok", notes=f"{skill_count} skills")
        print(_green(f"\n{args.project}: curator run completed ({skill_count} skills)."))
    else:
        state.finish_run(run_id, "failed", notes=_classify_run_failure(args.project, r.returncode))
    return r.returncode


# ─── runs ──────────────────────────────────────────────────────────────────


def cmd_runs(args) -> int:
    from rich.console import Console
    from rich.table import Table

    console = Console()
    state.init_db()
    runs = state.recent_runs(limit=args.limit, project_key=args.project)
    if not runs:
        _ui_header(console, "runs")
        console.print("No runs recorded yet.")
        return 0
    scope = args.project or "all projects"
    _ui_header(console, "runs", f"{scope} · latest {args.limit}")
    console.print()
    table = Table(show_header=True, header_style="bold", expand=False, box=None, padding=(0, 2, 0, 0))
    table.add_column("started")
    table.add_column("project", style="bold")
    table.add_column("kind")
    table.add_column("status")
    table.add_column("notes", style="dim")
    for r in runs:
        t = (r["started_at"] or "?")[:19]
        status = r["status"]
        table.add_row(t, r["project_key"], r["kind"], _rich_status(status), r["notes"] or "")
    console.print(table)
    return 0


# ─── learn ─────────────────────────────────────────────────────────────────


def cmd_learn(args) -> int:
    """Fast feedback loop: incremental analyze + lightweight curate.

    Default mode runs `analyze` (only new days) then `curate --regen-claude`
    (Stage 3 only — refreshes CLAUDE.md but keeps existing skills). This is
    the affordable "did watchmen catch my latest frustration?" loop, costs
    roughly $0.50, takes 5-10 minutes.

    `--full` runs the whole curator (Stage 1 finder + Stage 2 per-skill +
    Stage 3 CLAUDE.md). Costs $3-8, takes 30-60 min — only worth it when
    you expect new skill candidates to surface."""
    state.init_db()
    proj = state.get_project(args.project)
    if not proj:
        print(_yellow(f"'{args.project}' not tracked. Run `watchmen init` or `watchmen track`."))
        return 1

    progress = state.get_project_progress(args.project)
    new_prompts = progress.get("new_prompts_since_last_analysis", 0)
    last_day = progress.get("last_analyst_day") or "—"

    print(_bold(f"\nwatchmen learn — {args.project}\n"))
    print(f"  last analyst day: {last_day}")
    print(f"  new prompts since: {new_prompts}")
    print()

    if new_prompts == 0:
        print(_dim("  no new prompts to analyze."))
        if not args.full:
            print(_dim(f"  use --full to refresh CLAUDE.md anyway, or `watchmen show {args.project} CLAUDE.md` to view current."))
            return 0
    else:
        print(_bold(f"[1/2] Analyst (incremental from {last_day})…"))
        analyze_args = argparse.Namespace(
            project=args.project, full=False, repo=None, model=args.model,
        )
        rc = cmd_analyze(analyze_args)
        if rc != 0:
            print(_yellow(f"  analyst failed (rc={rc}); skipping curator"))
            return rc

    print()
    mode_label = "full curator" if args.full else "CLAUDE.md refresh only"
    print(_bold(f"[2/2] Curator — {mode_label}…"))
    curate_args = argparse.Namespace(
        project=args.project, regen_claude=not args.full, model=args.model,
        auto_install=True,  # Auto-install skills by default in learn workflow
    )
    rc = cmd_curate(curate_args)

    print()
    if rc == 0:
        print(_green("  ✓ learn complete"))
        print(_dim(f"  view: watchmen show {args.project} CLAUDE.md"))
        if not args.full:
            print(_dim(f"  for new skills: watchmen learn {args.project} --full"))
    return rc


# ─── metrics ───────────────────────────────────────────────────────────────


def cmd_metrics(args) -> int:
    from watchmen import metrics as _metrics
    from rich.console import Console
    from rich.table import Table

    if not args.project:
        return _cmd_metrics_global(args)

    rows = _metrics.daily_metrics(args.project, days=args.days)
    if not rows:
        print(f"No data for project '{args.project}'. Run `watchmen ingest` first?")
        return 1
    last7 = _metrics.summarize_window(rows, min(7, args.days))
    last30 = _metrics.summarize_window(rows, args.days)
    console = Console()

    # Sparklines for daily cost + prompts — give an at-a-glance shape of the
    # window. `rows` is ordered most-recent-first; reverse to read l→r as time
    # progresses.
    daily = list(reversed(rows))
    cost_series = [float(r.get("cost_usd", 0) or 0) for r in daily]
    prompts_series = [float(r.get("prompts", 0) or 0) for r in daily]
    if any(cost_series):
        console.print(f"  [yellow]cost[/]    {_sparkline(cost_series)}  [dim]peak ${max(cost_series):.2f}/d · total ${last30['cost_usd']:.2f}[/]")
    if any(prompts_series):
        console.print(f"  [cyan]prompts[/] {_sparkline(prompts_series)}  [dim]peak {int(max(prompts_series))}/d · total {last30['prompts']}[/]")
    console.print()

    rollup = Table(title=f"{args.project} — {args.days}-day rollup", show_header=True, header_style="bold magenta")
    rollup.add_column("Metric")
    rollup.add_column("7d", justify="right")
    rollup.add_column(f"{args.days}d", justify="right")
    rollup.add_row("Sessions", str(last7["sessions"]), str(last30["sessions"]))
    rollup.add_row("Prompts", str(last7["prompts"]), str(last30["prompts"]))
    rollup.add_row("Tool errors", str(last7["tool_errors"]), str(last30["tool_errors"]))
    rollup.add_row("Input tokens", f"{last7['input_tokens']:,}", f"{last30['input_tokens']:,}")
    rollup.add_row("Output tokens", f"{last7['output_tokens']:,}", f"{last30['output_tokens']:,}")
    rollup.add_row("Cost (USD)", f"${last7['cost_usd']:.2f}", f"${last30['cost_usd']:.2f}")
    rollup.add_row("Suggestions fired", str(last7["suggestions_fired"]), str(last30["suggestions_fired"]))
    rollup.add_row("Uptake", str(last7["uptake"]), str(last30["uptake"]))
    console.print(rollup)
    # Per-adapter breakdown — quick visibility into where this project's
    # sessions came from (claude_code vs codex vs pi vs cursor).
    breakdown = _adapter_breakdown(args.project)
    if breakdown:
        adapt = Table(show_header=True, header_style="bold cyan", expand=False)
        adapt.add_column("adapter")
        adapt.add_column("sessions", justify="right")
        for agent in ("claude_code", "codex", "pi", "cursor"):
            adapt.add_row(agent, str(breakdown.get(agent, 0)))
        console.print(adapt)
    console.print(f"\n  full daily breakdown: {config.viewer_base_url()}/p/{args.project}/metrics")
    return 0


def _cmd_metrics_global(args) -> int:
    """Global rollup across all tracked projects. Aggregates tokens/cost from
    metrics.daily_metrics per project + adapter session counts from corpus.db."""
    from watchmen import metrics as _metrics
    from rich.console import Console
    from rich.table import Table
    state.init_db()
    projects = state.list_projects()
    if not projects:
        print(_dim("No projects tracked yet — run `watchmen init`."))
        return 1
    console = Console()

    rows = []
    totals = {"sessions": 0, "prompts": 0, "input": 0, "output": 0, "cost": 0.0}
    for p in projects:
        key = p["project_key"]
        daily = _metrics.daily_metrics(key, days=args.days)
        summary = _metrics.summarize_window(daily, args.days) if daily else None
        if summary:
            totals["sessions"] += summary["sessions"]
            totals["prompts"]  += summary["prompts"]
            totals["input"]    += summary["input_tokens"]
            totals["output"]   += summary["output_tokens"]
            totals["cost"]     += summary["cost_usd"]
            rows.append((key, summary))
    rows.sort(key=lambda r: r[1]["cost_usd"], reverse=True)

    # Bars instead of columns of numbers — eyes track relative spend much
    # faster from horizontal bars. Heaviest user gets a full-width bar.
    header = (
        f"\n[bold]Global rollup — {args.days}d[/]  "
        f"[dim]{len(rows)} projects · {totals['sessions']:,} sessions · "
        f"${totals['cost']:.2f}[/]\n"
    )
    console.print(header)
    max_cost = max((s["cost_usd"] for _, s in rows), default=0.0)
    cost_tbl = Table(title="Cost by project", show_header=True, header_style="bold magenta", box=None, padding=(0, 1, 0, 1))
    cost_tbl.add_column("project", style="bold")
    cost_tbl.add_column("", width=30)
    cost_tbl.add_column("cost", justify="right")
    cost_tbl.add_column("share", justify="right")
    cost_tbl.add_column("sessions", justify="right")
    for key, s in rows:
        share = (s["cost_usd"] / totals["cost"] * 100) if totals["cost"] else 0
        bar = _bar(s["cost_usd"], max_cost, width=30)
        cost_tbl.add_row(
            key,
            f"[yellow]{bar}[/]",
            f"${s['cost_usd']:>9.2f}",
            f"{share:>3.0f}%",
            f"{s['sessions']:,}",
        )
    console.print(cost_tbl)

    adapter_rows = _metrics.adapter_breakdown_all(days=args.days)
    if adapter_rows:
        console.print()
        max_n = max(a["sessions"] for a in adapter_rows) or 1
        total_n = sum(a["sessions"] for a in adapter_rows)
        atbl = Table(title=f"By coding agent — last {args.days}d", show_header=True, header_style="bold cyan", box=None, padding=(0, 1, 0, 1))
        atbl.add_column("agent", style="bold")
        atbl.add_column("", width=28)
        atbl.add_column("sessions", justify="right")
        atbl.add_column("share", justify="right")
        atbl.add_column("projects", justify="right")
        atbl.add_column("prompts", justify="right")
        atbl.add_column("tool errs", justify="right")
        atbl.add_column("cost", justify="right")
        for a in adapter_rows:
            pct = (a["sessions"] / total_n * 100) if total_n else 0
            err_str = f"[red]{a['tool_errors']:,}[/]" if a["tool_errors"] else "0"
            cost_str = f"[yellow]${a['cost_usd']:.2f}[/]" if a["cost_usd"] else "$0.00"
            atbl.add_row(
                a["label"],
                f"[cyan]{_bar(a['sessions'], max_n, width=28)}[/]",
                f"{a['sessions']:,}",
                f"{pct:>3.0f}%",
                str(a["projects"]),
                f"{a['prompts']:,}",
                err_str,
                cost_str,
            )
        console.print(atbl)
    return 0
