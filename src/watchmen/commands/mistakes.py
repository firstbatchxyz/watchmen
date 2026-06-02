"""`watchmen mistakes` — the friction ledger from the corpus, in the terminal.

The CLI side of `watchmen.metrics.friction_ledger`. Recurring tool failures
grouped by *what went wrong* and ranked by how often & how recently they
recur — the "do I keep making the same mistake?" view:

  watchmen mistakes                  # global: every recurring mistake
  watchmen mistakes --project <key>  # scope to one project
  watchmen mistakes --days 30        # tighter window
  watchmen mistakes --json           # raw rows for scripting

This is the one exploration view that's also glanceable in a terminal (the
work matrix and swimlane stay viewer-only). The same mistake in different
words folds into one row via the error-signature normalizer, so the counts
reflect the *class* of mistake, not each phrasing.
"""

from __future__ import annotations

import json as _json

from watchmen import metrics as wm_metrics
from watchmen.ui import bold, dim, yellow


# Block-element sparkline: a recurrence trend you can read in a table cell.
_SPARK_TICKS = "▁▂▃▄▅▆▇█"


def _spark(weekly: list[int]) -> str:
    if not weekly or max(weekly) == 0:
        return dim("·" * len(weekly or [0]))
    hi = max(weekly)
    out = []
    for v in weekly:
        if v == 0:
            out.append(" ")
        else:
            idx = int((v / hi) * (len(_SPARK_TICKS) - 1))
            out.append(_SPARK_TICKS[idx])
    return "".join(out)


def _ago(days_since: int) -> str:
    if days_since <= 0:
        return "today"
    if days_since == 1:
        return "1d ago"
    return f"{days_since}d ago"


def cmd_mistakes(args) -> int:
    """Entry point for `watchmen mistakes [--project <key>] [--days N] [--json]`."""
    project = getattr(args, "project", None)
    days = getattr(args, "days", 90)
    limit = getattr(args, "limit", 25)
    data = wm_metrics.friction_ledger(days=days, project_key=project, limit=limit)

    if getattr(args, "json", False):
        print(_json.dumps(data, indent=2))
        return 0

    from rich.console import Console
    from rich.markup import escape
    from rich.table import Table

    console = Console()
    entries = data["entries"]
    if not entries:
        console.print(
            yellow(
                "No recurring tool failures captured"
                + (f" for project '{project}'" if project else "")
                + " in the window."
            )
        )
        console.print(
            dim("  Mistakes are logged once a tool call fails in a real session. "
                "Run `watchmen ingest` to refresh, and note older sessions only "
                "carry signatures after a full re-scan.")
        )
        return 0

    scope = f" — {project}" if project else ""
    console.print()
    console.print(bold(f"Recurring mistakes{scope}"))
    console.print(dim(
        f"  last {days} days  ·  {data['distinct']} recurring across "
        f"{data['signatured']} failures"
        + (f"  ·  {data['singletons']} one-offs not shown" if data["singletons"] else "")
    ))
    console.print()

    t = Table(header_style="cyan", show_lines=False, expand=False)
    t.add_column("mistake", max_width=64, overflow="fold")
    t.add_column("trend", justify="left")
    t.add_column("times", justify="right")
    t.add_column("sess", justify="right")
    t.add_column("repos", justify="right")
    t.add_column("last", justify="right")
    t.add_column("top tool", justify="left")
    for e in entries:
        # Signatures/tool names are raw corpus text — escape so a literal
        # "[errno 2]" isn't eaten as Rich console markup.
        t.add_row(
            escape(e["signature"]),
            _spark(e["spark"]),
            f"{e['occurrences']:,}",
            f"{e['sessions']:,}",
            f"{e['repos']:,}",
            _ago(e["days_since_last"]),
            escape(e["top_tool"]),
        )
    console.print(t)
    console.print()
    console.print(dim(
        "  Ranked by recurrence × recency. The same mistake in different words "
        "folds into one row."
    ))
    return 0
