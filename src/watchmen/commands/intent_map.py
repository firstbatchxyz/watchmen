"""`watchmen map` — build / warm the prompt-intent map cache.

The map itself is a viewer view (a 2-D scatter needs pan/zoom/hover to be
worth anything), so this command isn't a renderer — it's the pre-warm step.
Embedding and the t-SNE projection are the slow parts; running this once
populates `intent.db` so the `/map` page loads instantly afterward.

  watchmen map                     # build the global map cache
  watchmen map --project <key>     # build one project's map
  watchmen map --rebuild           # force a fresh projection
  watchmen map --days 90           # tighter window

Prints a one-line outcome summary (errored / rephrase / clean) so you get the
headline without opening the browser. Needs the optional `[map]` extra; prints
an install hint if it's absent.
"""

from __future__ import annotations

from watchmen.ui import bold, dim, yellow


def cmd_map(args) -> int:
    """Entry point for `watchmen map [--project <key>] [--days N] [--rebuild]`."""
    from watchmen import semantic

    project = getattr(args, "project", None)
    days = getattr(args, "days", 180)
    rebuild = getattr(args, "rebuild", False)

    from rich.console import Console
    console = Console()

    scope = f" — {project}" if project else ""
    console.print()
    console.print(bold(f"Building prompt-intent map{scope}"))
    console.print(dim(f"  last {days} days  ·  embedding locally, this can take a minute…"))

    try:
        data = semantic.intent_map(days=days, project_key=project, rebuild=rebuild)
    except semantic.MapDepsMissing as e:
        console.print()
        console.print(yellow(str(e)))
        return 1

    if not data["points"]:
        console.print()
        console.print(yellow(
            "No genuine prompts in the window"
            + (f" for project '{project}'" if project else "")
            + ". Run `watchmen ingest` to refresh the corpus."
        ))
        return 0

    oc = data["outcomes"]
    shown = f"{data['shown']:,}"
    total = f"{data['total_prompts']:,}"
    sampled = f"  (newest {shown} of {total})" if data["sampled"] else ""
    model_short = data["model"].split("/")[-1]
    console.print()
    console.print(
        f"  {bold(shown)} prompts mapped{sampled}  ·  model {dim(model_short)}"
    )
    console.print(
        f"  [red]●[/red] errored {oc['errored']:,}   "
        f"[yellow]●[/yellow] rephrase {oc['rephrase']:,}   "
        f"[blue]●[/blue] clean {oc['clean']:,}"
    )
    console.print()
    url = "/map" if not project else f"/p/{project}/map"
    console.print(dim(f"  Cached. Open the viewer at {url} to explore it."))
    return 0
