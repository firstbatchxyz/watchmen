"""Skill mesh and distillation command."""

from __future__ import annotations

import json
import sys
from dataclasses import asdict

from rich.console import Console

from watchmen.skillmesh import (
    SkillCluster,
    apply_distilled_candidates,
    build_distill_plan,
    build_semantic_distill_plan,
    render_plan_animation,
    render_plan_summary,
    stageable_distill_clusters,
    stage_distilled_candidates,
    write_distill_plan,
)
from watchmen.ui import dim, green, yellow


def _cluster_line(cluster: SkillCluster) -> str:
    if cluster.semantic_judgment:
        return (
            f"{cluster.proposed_slug}  "
            f"{cluster.semantic_judgment.similarity:.2f} "
            f"{cluster.semantic_judgment.merge_decision}/{cluster.semantic_judgment.risk}"
        )
    return f"{cluster.proposed_slug}  {cluster.score:.2f}"


def _cluster_summary(cluster: SkillCluster) -> str:
    lines = [
        cluster.proposed_slug,
        "",
        f"sources: {', '.join(cluster.members)}",
    ]
    if cluster.semantic_judgment:
        judgment = cluster.semantic_judgment
        lines.extend([
            f"similarity: {judgment.similarity:.2f}",
            f"relationship: {judgment.relationship}",
            f"decision: {judgment.merge_decision}/{judgment.risk}",
            "",
            judgment.rationale or "(no rationale)",
        ])
        if judgment.preserve:
            lines.append("")
            lines.append("preserve:")
            lines.extend(f"- {item}" for item in judgment.preserve[:5])
    else:
        lines.extend([
            f"local score: {cluster.score:.2f}",
            f"shared: {', '.join(cluster.shared_keywords[:8]) or '-'}",
        ])
    return "\n".join(lines)


def _pick_distill_clusters(plan, console: Console) -> list[str] | None:
    """Arrow/space picker with a live summary pane."""
    try:
        from prompt_toolkit import Application
        from prompt_toolkit.formatted_text import FormattedText
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.layout import Layout
        from prompt_toolkit.layout.containers import HSplit, VSplit, Window
        from prompt_toolkit.layout.controls import FormattedTextControl
        from prompt_toolkit.styles import Style
    except ImportError:
        console.print("[yellow]interactive merge picker needs prompt_toolkit/questionary installed[/]")
        return None

    candidates = stageable_distill_clusters(plan)
    if not candidates:
        return []

    selected = set(range(len(candidates)))
    cursor = 0
    kb = KeyBindings()

    def redraw(event) -> None:
        event.app.invalidate()

    @kb.add("up")
    @kb.add("k")
    def _(event) -> None:
        nonlocal cursor
        cursor = (cursor - 1) % len(candidates)
        redraw(event)

    @kb.add("down")
    @kb.add("j")
    def _(event) -> None:
        nonlocal cursor
        cursor = (cursor + 1) % len(candidates)
        redraw(event)

    @kb.add(" ")
    def _(event) -> None:
        if cursor in selected:
            selected.remove(cursor)
        else:
            selected.add(cursor)
        redraw(event)

    @kb.add("enter")
    def _(event) -> None:
        event.app.exit(result=[candidates[i].proposed_slug for i in sorted(selected)])

    @kb.add("escape")
    @kb.add("q")
    @kb.add("c-c")
    def _(event) -> None:
        event.app.exit(result=None)

    def list_fragments() -> FormattedText:
        fragments = [
            ("class:title", "distill merge picker\n"),
            ("class:help", "up/down move  space toggle  enter apply selected  q cancel\n\n"),
        ]
        for idx, cluster in enumerate(candidates):
            active = idx == cursor
            checked = "[x]" if idx in selected else "[ ]"
            pointer = ">" if active else " "
            style = "class:active" if active else "class:item"
            fragments.append((style, f"{pointer} {checked} {_cluster_line(cluster)}\n"))
        return FormattedText(fragments)

    def summary_fragments() -> FormattedText:
        summary = _cluster_summary(candidates[cursor])
        fragments = [("class:title", "selected skill summary\n"), ("", "\n")]
        for line in summary.splitlines():
            fragments.append(("class:summary", line + "\n"))
        fragments.append(("", "\n"))
        fragments.append(("class:warn", "Enter promotes selected drafts, archives originals, and blocklists source slugs.\n"))
        return FormattedText(fragments)

    root = HSplit([
        VSplit([
            Window(FormattedTextControl(list_fragments), width=72, always_hide_cursor=True),
            Window(width=2, char=" "),
            Window(FormattedTextControl(summary_fragments), always_hide_cursor=True),
        ]),
    ])
    app = Application(
        layout=Layout(root),
        key_bindings=kb,
        style=Style.from_dict({
            "title": "bold",
            "help": "ansibrightblack",
            "item": "",
            "active": "reverse",
            "summary": "",
            "warn": "ansiyellow",
        }),
        full_screen=True,
        mouse_support=False,
    )
    return app.run()


def _offer_interactive_apply(plan, console: Console) -> bool:
    if plan.semantic_model is None or not plan.clusters:
        return False
    if not stageable_distill_clusters(plan):
        return False
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return False
    try:
        import questionary
    except ImportError:
        console.print("[dim]interactive distill apply needs questionary; use --stage for non-interactive review[/]")
        return False

    answer = questionary.confirm(
        "Open merge picker now? Selected distilled skills will replace originals (archived).",
        default=False,
    ).ask()
    if not answer:
        return False

    selected = _pick_distill_clusters(plan, console)
    if selected is None:
        console.print("[dim]distill merge cancelled[/]")
        return False
    if not selected:
        console.print("[dim]no distill candidates selected[/]")
        return False

    result = apply_distilled_candidates(plan, selected)
    if not result.promoted:
        console.print("[dim]no selected distill candidates were applied[/]")
        return False
    console.print(f"[green]merged {len(result.promoted)} distilled skill(s)[/]")
    console.print(f"[dim]  promoted -> {', '.join(result.promoted)}[/]")
    if result.archived_sources:
        console.print(f"[dim]  archived originals -> {', '.join(result.archived_sources)}[/]")
    if result.archive_dir:
        console.print(f"[dim]  archive -> {result.archive_dir}[/]")
    if result.audit_path:
        console.print(f"[dim]  audit -> {result.audit_path}[/]")
    return True


def cmd_distill(args) -> int:
    """Inspect a project's created skills, find overlap, and propose merges.

    Default mode runs the semantic LLM rubric and is non-destructive.
    ``--stage`` writes merged draft skills under ``_pending/`` so the existing
    review flow remains the human approval gate.
    """
    console = Console()
    use_llm = not getattr(args, "local", False)
    threshold = getattr(args, "threshold", None)
    if threshold is None:
        threshold = 0.80 if use_llm else 0.28
    try:
        if use_llm:
            plan = build_semantic_distill_plan(
                args.project,
                model=getattr(args, "model", None),
                min_similarity=float(threshold),
                source_scope=str(getattr(args, "scope", "metadata")),
                console=console,
                show_visual=not getattr(args, "json", False),
            )
        else:
            plan = build_distill_plan(
                args.project,
                min_similarity=float(threshold),
                source_scope=str(getattr(args, "scope", "metadata")),
            )
    except FileNotFoundError as exc:
        print(yellow(str(exc)))
        print(dim(f"  run `watchmen curate {args.project}` first"))
        return 1
    except (KeyError, ValueError) as exc:
        print(yellow(str(exc)))
        return 1
    except Exception as exc:
        if use_llm:
            print(yellow(f"LLM similarity judge failed: {type(exc).__name__}: {exc}"))
            print(dim("  rerun with --local for the offline candidate mesh only"))
            return 1
        raise

    plan_path = write_distill_plan(plan)

    if getattr(args, "json", False):
        console.print_json(json.dumps(asdict(plan)))
    elif getattr(args, "animate", False):
        render_plan_animation(plan, console=console)
    else:
        render_plan_summary(plan, console=console)

    staged = []
    applied = False
    if getattr(args, "stage", False):
        staged = stage_distilled_candidates(plan)
        if staged:
            print(green(f"staged {len(staged)} distilled candidate(s) in _pending/"))
            for path in staged[:8]:
                print(dim(f"  wrote -> {path}"))
            if len(staged) > 8:
                print(dim(f"  ... {len(staged) - 8} more"))
            print(dim(f"  review with: watchmen review {args.project}"))
        else:
            print(dim("no merge clusters to stage"))
    elif not getattr(args, "json", False):
        applied = _offer_interactive_apply(plan, console)

    print(dim(f"  plan -> {plan_path}"))
    if plan.clusters and not staged and not applied:
        print(dim(f"  stage merged drafts with: watchmen distill {args.project} --stage"))
    return 0
