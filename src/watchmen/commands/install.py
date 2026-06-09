"""`watchmen install` command.

Symlink a project's curated skill bundles into the coding agents' discovery
dirs (``~/.claude/skills``, ``~/.codex/skills``) so the agent can actually
load and fire them. Bundles stay the source of truth; install just links.

    watchmen install <project>                 # all skills, all harnesses
    watchmen install <project> --skill foo      # one slug (repeatable)
    watchmen install <project> --harness codex  # one harness (repeatable)
    watchmen install <project> --force          # overwrite user-made targets
    watchmen install <project> --uninstall      # remove watchmen's links
    watchmen install <project> --list           # show status, change nothing
"""

from __future__ import annotations

from watchmen import skill_install as si
from watchmen.ui import bold, dim, green, red, yellow

_ACTION_STYLE = {
    "installed": green,
    "replaced": green,
    "migrated": green,
    "uninstalled": green,
    "skipped_conflict": yellow,
    "skipped_no_dir": dim,
    "not_installed": dim,
    "missing": red,
}


def _style(action: str, text: str) -> str:
    return _ACTION_STYLE.get(action, dim)(text)


def cmd_install(args) -> int:
    if getattr(args, "migrate", False):
        return _run_migrate()

    project = args.project
    if not project:
        print(red("a project key is required (or pass --migrate)"))
        return 1
    skills = si.bundle_skills(project)
    if not skills:
        print(red(f"no curated skills found for '{project}'"))
        print(dim("  (run `watchmen curate <project>` first, or check the project key with `watchmen show`)"))
        return 1

    slugs = [s.strip() for s in (args.skill or []) if s.strip()] or None
    harnesses = [h.strip().replace("-", "_") for h in (args.harness or []) if h.strip()] or None
    scope = getattr(args, "scope", "project") or "project"

    if getattr(args, "list", False):
        return _print_status(project, skills)

    if getattr(args, "uninstall", False):
        return _run_uninstall(project, skills, slugs, harnesses)

    results = si.install_project(project, harnesses=harnesses, slugs=slugs, scope=scope, force=args.force)
    if not results:
        print(yellow("nothing to install (no matching skills/harnesses)"))
        return 0

    changed = sum(1 for r in results if r.action in ("installed", "replaced"))
    conflicts = [r for r in results if r.action == "skipped_conflict"]
    no_repo = [r for r in results if r.action == "skipped_no_dir" and "no resolvable repo" in r.reason]
    print(bold(f"watchmen install {project}  {dim('· scope=' + scope)}"))
    for r in results:
        target = str(r.target) if r.target else "-"
        print(f"  {_style(r.action, r.action):<24} {r.slug}  {dim('→')} {r.harness}  {dim(target)}")
    print()
    print(f"{green(str(changed))} linked, {len(conflicts)} skipped")
    if conflicts:
        print(dim("  skipped targets already exist and weren't created by watchmen; rerun with --force to overwrite"))
    if no_repo:
        print(yellow(f"  {len(no_repo)} skipped: project scope needs a tracked repo on disk — `watchmen track`, or use --scope global"))
    if changed and scope == "project":
        # Repo-local links live in the working tree. We never edit .gitignore —
        # that's the user's call — but it's worth a one-line heads-up.
        print(dim("  tip: these live in <repo>/.claude/skills — add it to .gitignore if you'd rather not track the symlinks"))
    return 0


def _run_migrate() -> int:
    print(bold("watchmen install --migrate  ") + dim("global → repo-local"))
    results = si.migrate_to_project_scope()
    if not results:
        print(dim("  nothing to migrate (no global watchmen links, or all already project-scoped)"))
        return 0
    moved = sum(1 for r in results if r.action == "migrated")
    for r in results:
        target = str(r.target) if r.target else "-"
        note = f"  {dim(r.reason)}" if r.reason else ""
        print(f"  {_style(r.action, r.action):<24} {r.slug}  {dim('→')} {r.harness}  {dim(target)}{note}")
    print()
    print(f"{green(str(moved))} moved into their repos")
    return 0


def _run_uninstall(project, skills, slugs, harnesses) -> int:
    targets = harnesses if harnesses is not None else list(si.HARNESS_SKILL_DIRS)
    wanted = set(slugs) if slugs is not None else {s.slug for s in skills}
    print(bold(f"watchmen install {project} --uninstall"))
    removed = 0
    for slug in sorted(wanted):
        for harness in targets:
            res = si.uninstall_skill(slug, harness, project_key=project)
            if res.action == "uninstalled":
                removed += 1
            if res.action in ("uninstalled", "skipped_conflict"):
                print(f"  {_style(res.action, res.action):<24} {slug}  {dim('→')} {harness}")
    print()
    print(f"{green(str(removed))} removed")
    return 0


def _print_status(project, skills) -> int:
    installed = {(e["slug"], e["harness"]) for e in si.installed_targets(project)}
    harnesses = list(si.HARNESS_SKILL_DIRS)
    print(bold(f"watchmen install {project} --list"))
    print(dim(f"  {len(skills)} curated skills · harnesses: {', '.join(harnesses)}"))
    for s in skills:
        marks = []
        for h in harnesses:
            mark = green("✓") if (s.slug, h) in installed else dim("·")
            marks.append(f"{mark} {h}")
        print(f"  {s.slug:<32} {'  '.join(marks)}")
    return 0
