"""Per-harness file emitters for ``watchmen route`` recommendations.

When ``route`` lands a ``downshift`` / ``upshift`` / ``switch-harness``
decision, we don't stop at "you should swap" — we write the actual files
the harness reads at runtime, so the user runs `watchmen route` once and
their main agent natively delegates the skill to the recommended model.

Each harness has a different model-bearing artifact format and a
different dispatch syntax the SKILL.md body has to invoke.  The mapping
came out of the harness research pass:

  claude_code  ➜  ``<repo>/.claude/agents/<bucket>-router.md`` (YAML
                  frontmatter ``model:`` + system-prompt body).  Dispatch
                  via ``Task subagent_type=<bucket>-router``.

  codex        ➜  ``~/.codex/route-<bucket>.config.toml`` (TOML
                  ``model = "..."``).  Dispatch via
                  ``codex exec --profile-v2 route-<bucket>``.

  opencode     ➜  ``<repo>/.opencode/agents/<bucket>-router.md`` (YAML
                  frontmatter ``mode: subagent`` + ``model:``).  Dispatch
                  via ``@<bucket>-router`` mention.

  pi           ➜  ``~/.pi/agent/agents/<bucket>.md`` (YAML frontmatter
                  ``model:``).  Requires the opt-in subagent extension at
                  ``~/.pi/agent/extensions/subagent/``; without it, route
                  falls back to a body-only "run ``pi --model X``"
                  recommendation.

The SKILL.md body rewrite happens once per skill (not once per harness):
we insert a single ``<!-- watchmen-route:dispatch -->`` block that lists
each affected harness's dispatch sentence.  Future ``watchmen curate``
runs preserve this block via marker matching.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from watchmen.ax_dispatch import ax_agent_for, ax_exec_command
from watchmen.route import (
    RouteDecision,
    RouteResult,
    native_provider_for_harness,
    provider_supports_model,
)
from watchmen.state import get_project
from watchmen.util import bundle_dir


DISPATCH_MARKER_START = "<!-- watchmen-route:dispatch -->"
DISPATCH_MARKER_END = "<!-- /watchmen-route:dispatch -->"

PI_SUBAGENT_EXTENSION = Path.home() / ".pi" / "agent" / "extensions" / "subagent"


@dataclass
class RewriteOutcome:
    """One file watchmen wrote (or chose not to write)."""

    harness: str
    artifact_kind: str  # router | skill-body | (skipped)
    path: str
    action: str  # written | updated | skipped
    reason: str = ""


# ─── Public entry point ──────────────────────────────────────────────

def apply_route_rewrites(
    result: RouteResult,
    *,
    repo_root: str | None = None,
    dry_run: bool = False,
) -> list[RewriteOutcome]:
    """Materialise the route's actionable decisions.

    ``repo_root`` lets the caller override the tracked repo path lookup
    (mainly for tests).  When omitted we read it from watchmen's
    projects.json via ``state.get_project``.

    With ``dry_run=True`` we return the same list of outcomes but the
    ``action`` field is ``written`` (the path we *would* have touched)
    while no file changes actually happen.  This is what the CLI uses
    when the user passes ``--no-rewrite``.

    All outcomes are appended to ``<run_dir>/skill_rewrites.jsonl`` so
    the user can audit-and-revert by hand.
    """
    actionable = [
        d for d in result.decisions
        if d.label in {"downshift", "upshift", "switch-harness"}
        and d.recommended_model is not None
    ]
    if not actionable:
        return []

    repo = repo_root or _resolve_repo_root(result.config.project_key)
    skill_md_path = (
        bundle_dir(result.config.project_key)
        / "skills"
        / result.config.bucket
        / "SKILL.md"
    )

    outcomes: list[RewriteOutcome] = []
    dispatch_sentences: dict[str, str] = {}
    advisory_harnesses: set[str] = set()
    ax_routed_harnesses: set[str] = set()

    for decision in actionable:
        harness = decision.harness

        # Never stamp a model into a harness artifact unless that harness's
        # provider can actually run it. Two clauses, each covering a hole the
        # other misses — keep BOTH:
        #  - label == "switch-harness": the winner is another harness's current
        #    model. Catches multi-provider harnesses (opencode/pi) where
        #    provider_supports_model defaults to True and would otherwise let a
        #    foreign model through.
        #  - not provider_supports_model(...): catches a foreign --candidate the
        #    user injected that wins as downshift/upshift (so it's never labeled
        #    switch-harness) but still isn't runnable by a single-provider source.
        # The union over-skips only a rare same-family coincidence, which the
        # advisory line still surfaces — an accepted trade.
        native = native_provider_for_harness(harness)
        cross_runtime = (
            decision.label == "switch-harness"
            or not provider_supports_model(decision.recommended_model, native)
        )
        if cross_runtime:
            # Experimental: if AX is configured and the winning harness has an
            # AX wrapper, dispatch the skill there for real instead of only
            # advising. The *target* is reached uniformly via `ax exec`; only
            # the *source* needs a per-harness dispatcher (how that runtime
            # invokes the AX call), so support is keyed by source harness in
            # _AX_SOURCE_EMITTERS. Any gap — AX off, no wrapper for the target,
            # or an unsupported source — falls through to the advisory line
            # below (unchanged default behavior).
            ax_agent = ax_agent_for(decision.recommended_harness)
            ax_emitter = _AX_SOURCE_EMITTERS.get(harness) if ax_agent else None
            if ax_emitter is not None:
                outcome, sentence = ax_emitter(
                    decision=decision,
                    bucket=result.config.bucket,
                    repo_root=repo,
                    ax_agent=ax_agent,
                    dry_run=dry_run,
                )
                outcomes.append(outcome)
                dispatch_sentences[harness] = sentence
                ax_routed_harnesses.add(harness)
                continue

            advisory_harnesses.add(harness)
            dispatch_sentences[harness] = _advisory_sentence(
                decision, bucket=result.config.bucket
            )
            outcomes.append(
                RewriteOutcome(
                    harness=harness,
                    artifact_kind="advisory",
                    path="",
                    action="skipped",
                    reason=(
                        f"cross-runtime winner {decision.recommended_model!r} "
                        f"not runnable by {harness}; advised instead"
                    ),
                )
            )
            continue

        emitter = _HARNESS_EMITTERS.get(harness)
        if emitter is None:
            outcomes.append(
                RewriteOutcome(
                    harness=harness,
                    artifact_kind="router",
                    path="",
                    action="skipped",
                    reason=f"no rewriter for harness {harness!r}",
                )
            )
            continue
        outcome, dispatch_sentence = emitter(
            decision=decision,
            bucket=result.config.bucket,
            repo_root=repo,
            dry_run=dry_run,
        )
        outcomes.append(outcome)
        if dispatch_sentence:
            dispatch_sentences[harness] = dispatch_sentence

    # One SKILL.md body rewrite covering all affected harnesses.
    if dispatch_sentences and skill_md_path.exists():
        body_outcome = _rewrite_skill_body(
            skill_md_path=skill_md_path,
            dispatch_sentences=dispatch_sentences,
            decisions={d.harness: d for d in actionable},
            # Both advisory and AX-routed entries carry the model in their own
            # sentence, so suppress the independent `Recommended model:` line
            # (which would otherwise re-print the foreign model + label).
            advisory_harnesses=advisory_harnesses | ax_routed_harnesses,
            run_id=result.run_id,
            dry_run=dry_run,
        )
        outcomes.append(body_outcome)

    _audit_log(Path(result.run_dir) / "skill_rewrites.jsonl", outcomes)
    return outcomes


def _advisory_sentence(decision: RouteDecision, *, bucket: str) -> str:
    """Human-facing line for a cross-runtime winner we won't emit a file for.

    Two sub-cases: a switch-harness winner is already run by a known harness
    (point the user there); a non-runnable --candidate winner is run by no
    current harness (state that no route was emitted).
    """
    source = _harness_display_name(decision.harness)
    if decision.recommended_harness:
        target = _harness_display_name(decision.recommended_harness)
        return (
            f"For the `{bucket}` skill, run it on {target} — {source} can't run "
            f"`{decision.recommended_model}` natively."
        )
    return (
        f"For the `{bucket}` skill, the winning model "
        f"`{decision.recommended_model}` isn't runnable by {source} and isn't "
        "run by any current harness — no route emitted."
    )


# ─── claude-code ─────────────────────────────────────────────────────

def _emit_claude_code(
    *, decision: RouteDecision, bucket: str, repo_root: str | None, dry_run: bool
) -> tuple[RewriteOutcome, str]:
    """Write ``<repo>/.claude/agents/<bucket>-router.md`` if the project's
    source repo is known and writable; otherwise fall back to
    ``~/.claude/agents/watchmen-route-<bucket>.md`` so the user's global
    namespace isn't polluted with un-prefixed entries.
    """
    name = f"{bucket}-router"
    body = _claude_agent_body(decision, bucket=bucket)
    path, fell_back = _pick_router_path(
        repo_local=Path(repo_root) / ".claude" / "agents" / f"{name}.md" if repo_root else None,
        user_global=Path.home() / ".claude" / "agents" / f"watchmen-route-{bucket}.md",
    )
    action = _write_file(path, body, dry_run=dry_run)
    sentence = (
        f"In Claude Code, dispatch via the Task tool with "
        f"`subagent_type=\"{name if not fell_back else 'watchmen-route-' + bucket}\"` "
        f"so the subagent runs under `{decision.recommended_model}`."
    )
    return (
        RewriteOutcome(
            harness="claude_code",
            artifact_kind="router",
            path=str(path),
            action=action,
            reason="fallback to user-global namespace" if fell_back else "",
        ),
        sentence,
    )


def _claude_agent_body(decision: RouteDecision, *, bucket: str) -> str:
    name = f"{bucket}-router"
    return (
        "---\n"
        f"name: {name}\n"
        f"description: Watchmen-routed subagent for the `{bucket}` skill bucket. {decision.note}\n"
        f"model: {decision.recommended_model}\n"
        "tools: '*'\n"
        "---\n"
        "\n"
        f"# {name}\n"
        "\n"
        f"You execute the `{bucket}` skill on behalf of the main agent. Read "
        f"`bundles/<project>/skills/{bucket}/SKILL.md` for the operational "
        "guidance, then carry the work out yourself.  Return a concise summary "
        "to the main agent when done.\n"
        "\n"
        f"Watchmen routed this skill to `{decision.recommended_model}` because "
        f"a comparison run against `{decision.current_model}` showed: "
        f"{decision.note}\n"
    )


# ─── AX cross-runtime dispatch (experimental) ────────────────────────

def _emit_ax_dispatch_claude_code(
    *,
    decision: RouteDecision,
    bucket: str,
    repo_root: str | None,
    ax_agent: str,
    dry_run: bool,
) -> tuple[RewriteOutcome, str]:
    """Emit a Claude Code subagent that delegates the skill to the winning
    harness through AX, instead of a (non-runnable) native router pinned to a
    foreign model. The subagent shells out to ``ax exec --agent <target>``; the
    ``[model]`` / ``[workspace]`` headers ride inside ``--input`` since the CLI
    has no flags for them. Same artifact path as the native router so it
    replaces, rather than stacks with, the would-be broken file.
    """
    target = _harness_display_name(decision.recommended_harness)
    name = f"{bucket}-router"
    command = ax_exec_command(
        agent=ax_agent,
        model=decision.recommended_model,
        workspace=repo_root,
    )
    body = _ax_dispatch_body(
        decision, bucket=bucket, target=target, command=command
    )
    path, fell_back = _pick_router_path(
        repo_local=Path(repo_root) / ".claude" / "agents" / f"{name}.md" if repo_root else None,
        user_global=Path.home() / ".claude" / "agents" / f"watchmen-route-{bucket}.md",
    )
    action = _write_file(path, body, dry_run=dry_run)
    dispatch_name = name if not fell_back else f"watchmen-route-{bucket}"
    sentence = (
        f"In Claude Code, dispatch via the Task tool with "
        f"`subagent_type=\"{dispatch_name}\"`; it routes `{bucket}` to {target} "
        f"via AX (`ax exec --agent {ax_agent}`)."
    )
    return (
        RewriteOutcome(
            harness="claude_code",
            artifact_kind="ax-router",
            path=str(path),
            action=action,
            reason=(
                f"AX dispatch to {decision.recommended_harness}"
                + (" (user-global fallback)" if fell_back else "")
            ),
        ),
        sentence,
    )


def _ax_dispatch_body(
    decision: RouteDecision, *, bucket: str, target: str, command: str
) -> str:
    name = f"{bucket}-router"
    # No `model:` line: the orchestrating subagent runs under Claude's default
    # and only brokers the AX call — the real work happens on `target` under
    # `recommended_model`, never on a Claude model.
    return (
        "---\n"
        f"name: {name}\n"
        f"description: Watchmen-routed (AX): delegate the `{bucket}` skill to "
        f"{target}. {decision.note}\n"
        "tools: '*'\n"
        "---\n"
        "\n"
        f"# {name}\n"
        "\n"
        f"Watchmen determined the `{bucket}` skill runs better on {target} than "
        "on Claude Code, so you delegate it there through AX (google/ax) rather "
        "than running it yourself.\n"
        "\n"
        "When the main agent hands you a task for this skill:\n"
        "\n"
        f"1. Read `bundles/<project>/skills/{bucket}/SKILL.md` for the "
        "operational guidance.\n"
        "2. Run it on "
        f"{target} via AX, replacing `<TASK>` with the concrete request "
        "(include the SKILL.md guidance the work needs):\n"
        "\n"
        f"   {command}\n"
        "\n"
        "3. Return AX's output to the main agent verbatim.\n"
        "\n"
        f"Do not attempt the work under a Claude model — the route picked "
        f"{target} (`{decision.recommended_model}`) because: {decision.note}\n"
    )


def _emit_ax_dispatch_codex(
    *,
    decision: RouteDecision,
    bucket: str,
    repo_root: str | None,
    ax_agent: str,
    dry_run: bool,
) -> tuple[RewriteOutcome, str]:
    """Codex has no subagent-file mechanism (its native artifact is a model
    profile, which only overrides the model — it can't invoke another runtime).
    So the cross-runtime dispatch lives entirely in the SKILL.md dispatch
    sentence: the Codex session runs ``ax exec`` itself during the skill. No
    profile file is written.
    """
    target = _harness_display_name(decision.recommended_harness)
    command = ax_exec_command(
        agent=ax_agent,
        model=decision.recommended_model,
        workspace=repo_root,
    )
    sentence = (
        f"In Codex, the `{bucket}` skill is routed to {target} via AX. Run the "
        f"skill by executing `{command}` (replace `<TASK>` with the concrete "
        "request, including the SKILL.md guidance the work needs) and use AX's "
        "output as the result — do not run it under a Codex model."
    )
    return (
        RewriteOutcome(
            harness="codex",
            artifact_kind="ax-dispatch",
            path="",  # inline in SKILL.md; no per-harness file
            action="inline",
            reason=f"AX dispatch to {decision.recommended_harness}",
        ),
        sentence,
    )


# Source harness -> AX dispatcher. The target is reached uniformly through
# `ax exec`; only the entry point differs per source runtime. claude_code
# writes a subagent file; codex inlines the call into SKILL.md. Sources absent
# here (opencode/pi) fall back to the advisory path.
_AX_SOURCE_EMITTERS = {
    "claude_code": _emit_ax_dispatch_claude_code,
    "codex": _emit_ax_dispatch_codex,
}


# ─── codex ───────────────────────────────────────────────────────────

def _emit_codex(
    *, decision: RouteDecision, bucket: str, repo_root: str | None, dry_run: bool
) -> tuple[RewriteOutcome, str]:
    """Write ``$CODEX_HOME/route-<bucket>.config.toml`` (defaults to
    ``~/.codex/``).  Codex's ``--profile-v2 <name>`` layers this file on
    top of ``config.toml`` so only the keys we set (just ``model`` in v1)
    differ from the user's defaults.
    """
    profile_name = f"route-{bucket}"
    codex_home = Path.home() / ".codex"
    path = codex_home / f"{profile_name}.config.toml"
    body = _codex_profile_body(decision, profile_name=profile_name)
    action = _write_file(path, body, dry_run=dry_run)
    sentence = (
        f"In codex, run the skill with "
        f"`codex exec --profile-v2 {profile_name}` so the agent uses "
        f"`{decision.recommended_model}`."
    )
    return (
        RewriteOutcome(
            harness="codex",
            artifact_kind="router",
            path=str(path),
            action=action,
        ),
        sentence,
    )


def _codex_profile_body(decision: RouteDecision, *, profile_name: str) -> str:
    # Stripped-bare profile file: only override what we have to.  We
    # deliberately don't carry the user's reasoning_effort / personality
    # forward — the base config still wins on every key we *don't* set.
    return (
        f"# watchmen route profile: {profile_name}\n"
        f"# Generated {datetime.now(timezone.utc).isoformat()}\n"
        f"# Reason: {decision.note}\n"
        "\n"
        f"model = \"{decision.recommended_model}\"\n"
    )


# ─── opencode ────────────────────────────────────────────────────────

def _emit_opencode(
    *, decision: RouteDecision, bucket: str, repo_root: str | None, dry_run: bool
) -> tuple[RewriteOutcome, str]:
    """Write a subagent file at ``<repo>/.opencode/agents/<bucket>-router.md``
    (project-local) or ``~/.config/opencode/agents/watchmen-route-<bucket>.md``
    (user-global fallback)."""
    name = f"{bucket}-router"
    body = _opencode_agent_body(decision, bucket=bucket)
    path, fell_back = _pick_router_path(
        repo_local=Path(repo_root) / ".opencode" / "agents" / f"{name}.md" if repo_root else None,
        user_global=Path.home() / ".config" / "opencode" / "agents" / f"watchmen-route-{bucket}.md",
    )
    action = _write_file(path, body, dry_run=dry_run)
    sentence = (
        f"In opencode, dispatch via `@{name if not fell_back else 'watchmen-route-' + bucket}` "
        f"to run the skill under `{decision.recommended_model}`."
    )
    return (
        RewriteOutcome(
            harness="opencode",
            artifact_kind="router",
            path=str(path),
            action=action,
            reason="fallback to user-global namespace" if fell_back else "",
        ),
        sentence,
    )


def _opencode_agent_body(decision: RouteDecision, *, bucket: str) -> str:
    name = f"{bucket}-router"
    return (
        "---\n"
        f"description: Watchmen-routed subagent for the `{bucket}` skill bucket. {decision.note}\n"
        "mode: subagent\n"
        f"model: {decision.recommended_model}\n"
        "permission:\n"
        "  bash: allow\n"
        "  read: allow\n"
        "  edit: allow\n"
        "---\n"
        "\n"
        f"# {name}\n"
        "\n"
        f"Execute the `{bucket}` skill on the main agent's behalf.  Read the "
        f"skill body from `.claude/skills/{bucket}/SKILL.md` (opencode reads "
        "this path natively).  Return a concise summary when done.\n"
    )


# ─── pi.dev ──────────────────────────────────────────────────────────

def _emit_pi(
    *, decision: RouteDecision, bucket: str, repo_root: str | None, dry_run: bool
) -> tuple[RewriteOutcome, str]:
    """Two modes:

      - Full native (subagent extension installed at the conventional
        path): write ``~/.pi/agent/agents/<bucket>.md`` with the
        recommended ``model:`` and produce a Task-dispatch sentence the
        extension's registered tool can pick up.

      - Degraded (extension not installed): skip the file, produce a
        body-only "run ``pi --model X`` for this skill" sentence.

    The user can install the extension explicitly later; on the next
    ``watchmen route`` run we'll move them to full native automatically.
    """
    if not PI_SUBAGENT_EXTENSION.exists():
        sentence = (
            f"In pi.dev, run the skill with `pi --model {decision.recommended_model}` "
            "for the recommended model (native subagent dispatch requires the "
            "opt-in subagent extension at `~/.pi/agent/extensions/subagent/`)."
        )
        return (
            RewriteOutcome(
                harness="pi",
                artifact_kind="router",
                path="",
                action="skipped",
                reason="pi subagent extension not installed; body-only fallback",
            ),
            sentence,
        )

    name = bucket
    path = Path.home() / ".pi" / "agent" / "agents" / f"{name}.md"
    body = _pi_agent_body(decision, bucket=bucket)
    action = _write_file(path, body, dry_run=dry_run)
    sentence = (
        f"In pi.dev, dispatch the `{name}` subagent (model "
        f"`{decision.recommended_model}`) via the subagent extension's Task tool."
    )
    return (
        RewriteOutcome(
            harness="pi",
            artifact_kind="router",
            path=str(path),
            action=action,
        ),
        sentence,
    )


def _pi_agent_body(decision: RouteDecision, *, bucket: str) -> str:
    return (
        "---\n"
        f"name: {bucket}\n"
        f"description: Watchmen-routed subagent for the `{bucket}` skill bucket.\n"
        f"model: {decision.recommended_model}\n"
        "---\n"
        "\n"
        f"# {bucket}\n"
        "\n"
        f"Execute the `{bucket}` skill on the main agent's behalf.  Watchmen "
        f"routed it to `{decision.recommended_model}` because "
        f"{decision.note}.\n"
    )


# ─── SKILL.md body rewrite ───────────────────────────────────────────

def _rewrite_skill_body(
    *,
    skill_md_path: Path,
    dispatch_sentences: dict[str, str],
    decisions: dict[str, RouteDecision],
    advisory_harnesses: set[str],
    run_id: str,
    dry_run: bool,
) -> RewriteOutcome:
    """Insert / replace the dispatch block in SKILL.md.

    The block sits between ``<!-- watchmen-route:dispatch -->`` and
    ``<!-- /watchmen-route:dispatch -->`` markers so future ``curate``
    regenerations can preserve it without merging logic.  If the markers
    aren't present, we append the block at the end of the body (after
    the closing ``---`` of the frontmatter).
    """
    existing = skill_md_path.read_text(encoding="utf-8")
    block = _build_dispatch_block(
        dispatch_sentences=dispatch_sentences,
        decisions=decisions,
        advisory_harnesses=advisory_harnesses,
        run_id=run_id,
    )
    if DISPATCH_MARKER_START in existing and DISPATCH_MARKER_END in existing:
        new_body = re.sub(
            rf"{re.escape(DISPATCH_MARKER_START)}.*?{re.escape(DISPATCH_MARKER_END)}",
            block,
            existing,
            count=1,
            flags=re.DOTALL,
        )
        action = "updated" if not dry_run else "would-update"
    else:
        # Append after the body.  If the file ends without a trailing
        # newline (it always should, but be defensive), add one.
        sep = "" if existing.endswith("\n") else "\n"
        new_body = existing + sep + "\n" + block + "\n"
        action = "written" if not dry_run else "would-write"

    if not dry_run:
        skill_md_path.write_text(new_body, encoding="utf-8")

    return RewriteOutcome(
        harness="(all)",
        artifact_kind="skill-body",
        path=str(skill_md_path),
        action=action,
    )


def _build_dispatch_block(
    *,
    dispatch_sentences: dict[str, str],
    decisions: dict[str, RouteDecision],
    advisory_harnesses: set[str],
    run_id: str,
) -> str:
    lines = [
        DISPATCH_MARKER_START,
        "",
        "## Watchmen route",
        "",
        f"_Generated by `watchmen route` (`{run_id}`)._",
        "",
    ]
    for harness, sentence in dispatch_sentences.items():
        decision = decisions.get(harness)
        if decision is None:
            continue
        lines.append(f"### {_harness_display_name(harness)}")
        lines.append("")
        lines.append(sentence)
        # Advisory harnesses can't run the winner, so don't print a
        # "Recommended model: <foreign>" line that implies they will.
        if harness not in advisory_harnesses:
            if decision.cost_vs_current is not None:
                lines.append(
                    f"_Recommended model: `{decision.recommended_model}` "
                    f"({decision.label}, {decision.cost_vs_current:.2f}× cost vs "
                    f"`{decision.current_model}`)._"
                )
            else:
                lines.append(
                    f"_Recommended model: `{decision.recommended_model}` "
                    f"({decision.label})._"
                )
        lines.append("")
    lines.append(DISPATCH_MARKER_END)
    return "\n".join(lines)


def _harness_display_name(harness: str) -> str:
    return {
        "claude_code": "Claude Code",
        "codex": "Codex",
        "opencode": "OpenCode",
        "pi": "pi.dev",
    }.get(harness, harness)


# ─── Helpers ─────────────────────────────────────────────────────────

def _write_file(path: Path, body: str, *, dry_run: bool) -> str:
    """Write file or report what would happen.  Existing-content match
    is reported as ``unchanged`` so re-running route is a true no-op."""
    if path.exists():
        existing = path.read_text(encoding="utf-8")
        if existing == body:
            return "unchanged"
        if dry_run:
            return "would-update"
        path.write_text(body, encoding="utf-8")
        return "updated"
    if dry_run:
        return "would-write"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return "written"


def _pick_router_path(
    *, repo_local: Path | None, user_global: Path
) -> tuple[Path, bool]:
    """Prefer the repo-local path when the caller knows the repo root
    and the parent dir is writable; otherwise fall back to user-global
    with a watchmen-prefix.  Second return is ``True`` if we fell back."""
    if repo_local is not None:
        parent = repo_local.parent
        # Try to create parent; if anything fails (permission, missing
        # repo) we fall through to user-global.
        try:
            parent.mkdir(parents=True, exist_ok=True)
            return repo_local, False
        except OSError:
            pass
    return user_global, True


def _resolve_repo_root(project_key: str) -> str | None:
    proj = get_project(project_key)
    return proj.get("source_repo") if proj else None


def _audit_log(path: Path, outcomes: list[RewriteOutcome]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for outcome in outcomes:
            fh.write(
                json.dumps(
                    {
                        "harness": outcome.harness,
                        "artifact_kind": outcome.artifact_kind,
                        "path": outcome.path,
                        "action": outcome.action,
                        "reason": outcome.reason,
                    },
                    sort_keys=True,
                )
                + "\n"
            )


# ─── Harness emitter registry ────────────────────────────────────────

_HARNESS_EMITTERS = {
    "claude_code": _emit_claude_code,
    "codex": _emit_codex,
    "opencode": _emit_opencode,
    "pi": _emit_pi,
}
