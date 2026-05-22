"""`watchmen goals` — surface codex goal usage from the corpus.

  watchmen goals                  # global table: per-project rollup
  watchmen goals --project <key>  # detail: status mix + per-goal listing

Codex-only in v1. CC TodoWrite ingestion is deferred until usage exists
on real machines (see goals.py docstring + issue #78).
"""

from __future__ import annotations

from watchmen import goals as wm_goals
from watchmen.ui import bold, dim, yellow


def _money(x: float) -> str:
    return f"${x:,.2f}"


def _hms(seconds: int) -> str:
    if seconds <= 0:
        return "—"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    h, rem = divmod(seconds, 3600)
    m, _ = divmod(rem, 60)
    return f"{h}h{m:02d}m"


def _tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1e6:.1f}M"
    if n >= 1000:
        return f"{n / 1000:.0f}k"
    return str(n)


def _short_objective(text: str, width: int = 60) -> str:
    text = text.replace("\n", " ").strip()
    if len(text) <= width:
        return text
    return text[: width - 1] + "…"


def _status_color(status: str) -> str:
    return {
        "complete": "green",
        "active": "cyan",
        "paused": "yellow",
        "budget_limited": "red",
    }.get(status, "white")


def cmd_goals(args) -> int:
    """Entry point for `watchmen goals [--project <key>]`."""
    from rich.console import Console
    from rich.table import Table

    console = Console()
    project = getattr(args, "project", None)
    if project:
        return _render_project_detail(console, project)
    return _render_overview(console, Table)


def _render_overview(console, Table) -> int:
    t = wm_goals.totals()
    if t["goal_count"] == 0:
        console.print()
        console.print(yellow("No codex goals tracked yet."))
        console.print(dim(
            "  This surfaces data from `~/.codex/state_*.sqlite::thread_goals`.\n"
            "  Goals appear once you've used codex 0.133.0+ with a thread that "
            "set an objective.\n"
            "  Run `watchmen ingest` after creating a goal in codex to refresh."
        ))
        return 0

    console.print()
    console.print(bold("Codex goals"))
    bits = [f"{t['goal_count']:,} total", f"{_money(t['total_cost_usd'])} total"]
    if t["completed"]:
        bits.append(f"{t['completed']} complete")
    if t["active"]:
        bits.append(f"{t['active']} active")
    if t["paused"]:
        bits.append(f"{t['paused']} paused")
    if t["budget_limited"]:
        bits.append(f"[red]{t['budget_limited']} budget-limited[/red]")
    console.print(dim("  " + "  ·  ".join(bits)))
    console.print()

    rows = wm_goals.aggregate_per_project()
    table = Table(title="By project", title_style="bold", header_style="cyan",
                  show_lines=False, expand=False)
    table.add_column("project")
    table.add_column("goals", justify="right")
    table.add_column("done", justify="right")
    table.add_column("active", justify="right")
    table.add_column("budget", justify="right")
    table.add_column("tokens", justify="right")
    table.add_column("cost", justify="right")
    for r in rows[:20]:
        table.add_row(
            r.project_key,
            f"{r.goal_count:,}",
            f"[green]{r.completed}[/green]" if r.completed else "—",
            f"[cyan]{r.active}[/cyan]" if r.active else "—",
            f"[red]{r.budget_limited}[/red]" if r.budget_limited else "—",
            _tokens(r.total_tokens_used),
            _money(r.total_cost_usd),
        )
    console.print(table)
    console.print()
    console.print(dim(
        "  `watchmen goals --project <key>` for per-goal detail in one project."
    ))
    return 0


def _render_project_detail(console, project_key: str) -> int:
    from rich.table import Table
    from watchmen import state

    # Unlike `subagents --project`, goal data flows from codex's own
    # `~/.codex/.../threads.cwd` field — it exists independent of whether
    # the user has registered the project with `watchmen track`. Honor a
    # tracked project key when one matches (gives the user the nice repo
    # display), but fall through to a direct project_dir substring match
    # for untracked dirs codex naturally writes goals into.
    p = state.get_project(project_key)
    source_repo = p["source_repo"] if p else project_key
    goals = wm_goals.list_for_project(project_key, source_repo, limit=50)

    console.print()
    console.print(bold(f"Codex goals — {project_key}"))
    if p is None and goals:
        console.print(dim(
            "  (not a tracked watchmen project — matched by codex cwd substring)"
        ))
    if not goals:
        console.print(dim("  No codex goals captured for this project."))
        if p is None:
            console.print(dim(
                "  `watchmen list` shows your tracked projects. Goals also show "
                "up for any codex thread's cwd containing this substring."
            ))
        return 0

    status_counts = {s: 0 for s in (
        "complete", "active", "paused", "blocked", "usage_limited", "budget_limited",
    )}
    total_cost = 0.0
    for g in goals:
        status_counts[g.status] = status_counts.get(g.status, 0) + 1
        total_cost += g.cost_usd
    bits = [
        f"{len(goals)} goals", f"{_money(total_cost)} total",
        f"[green]{status_counts['complete']} done[/green]",
        f"[cyan]{status_counts['active']} active[/cyan]",
    ]
    if status_counts["paused"]:
        bits.append(f"[yellow]{status_counts['paused']} paused[/yellow]")
    if status_counts["blocked"]:
        bits.append(f"[yellow]{status_counts['blocked']} blocked[/yellow]")
    if status_counts["usage_limited"]:
        bits.append(f"[red]{status_counts['usage_limited']} usage-limited[/red]")
    if status_counts["budget_limited"]:
        bits.append(f"[red]{status_counts['budget_limited']} budget-limited[/red]")
    console.print(dim("  " + "  ·  ".join(bits)))
    console.print()

    t = Table(header_style="cyan", show_lines=False, expand=False)
    t.add_column("objective")
    t.add_column("status")
    t.add_column("tokens", justify="right")
    t.add_column("budget", justify="right")
    t.add_column("time", justify="right")
    t.add_column("cost", justify="right")
    for g in goals:
        budget_str = _tokens(g.token_budget) if g.token_budget else "—"
        t.add_row(
            _short_objective(g.objective),
            f"[{_status_color(g.status)}]{g.status}[/{_status_color(g.status)}]",
            _tokens(g.tokens_used),
            budget_str,
            _hms(g.time_used_seconds),
            _money(g.cost_usd),
        )
    console.print(t)
    return 0
