"""Install curated skill bundles into coding-agent discovery paths.

The curator writes skills to ``BUNDLES_DIR/<project_key>/skills/<slug>/``, but
coding agents only discover skills from their own directories
(``~/.claude/skills/``, ``~/.codex/skills/``). Until a curated skill is
installed there, the agent never sees it and it can never fire. This module
bridges that gap.

Install is **symlink-based**: ``~/.claude/skills/<slug>`` points back at the
bundle skill directory, so the bundle stays the single source of truth and
curator edits propagate without re-copying. A small manifest under
``WATCHMEN_HOME`` records every link watchmen creates, so uninstall is precise
and a skill directory the user made by hand is never touched.

Conflict policy: a target slug that watchmen created (recorded in the manifest,
or a symlink already pointing into ``BUNDLES_DIR``) is replaced on reinstall; a
target the user created themselves is skipped unless ``force=True``.

Scope: a skill installs either ``global`` (the shared ``~/.claude/skills``
dir, visible in every repo) or ``project`` (``<source_repo>/.claude/skills``,
visible only inside that repo). Project scope is the default for the
orchestrators so a project's curated skills don't leak into unrelated repos as
agent suggestions (#125).

Surface:
    bundle_skills(project_key)                       -> list[BundleSkill]
    harness_skill_dir(harness)                       -> Path | None  (global base)
    skill_base(harness, scope=, repo=, project_key=) -> Path | None
    install_skill(skill, harness, scope=, ...)       -> InstallResult
    install_project(project_key, scope=, ...)        -> list[InstallResult]
    uninstall_skill(slug, harness, project_key=)     -> InstallResult
    migrate_to_project_scope()                       -> list[InstallResult]
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from watchmen.paths import BUNDLES_DIR, WATCHMEN_HOME

# Global discovery directories per harness. Only harnesses that read a flat
# `<dir>/<slug>/SKILL.md` layout belong here; multi-provider harnesses without
# a local skill dir are intentionally absent (harness_skill_dir returns None).
HARNESS_SKILL_DIRS: dict[str, Path] = {
    "claude_code": Path.home() / ".claude" / "skills",
    "codex": Path.home() / ".codex" / "skills",
}

# Per-harness config subdir, used to build the REPO-LOCAL skill dir under a
# project's source repo (``<repo>/.claude/skills/`` etc.). This is how a
# project-scoped install keeps a project's curated skills visible only inside
# that repo, instead of polluting the global dir in every other repo (#125).
_HARNESS_SUBDIR: dict[str, str] = {
    "claude_code": ".claude",
    "codex": ".codex",
}

MANIFEST_PATH = WATCHMEN_HOME / "install_manifest.json"

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
_KV_RE = re.compile(r"^([a-zA-Z_][a-zA-Z0-9_-]*)\s*:\s*(.*)$")


@dataclass
class BundleSkill:
    slug: str
    name: str
    description: str
    skill_dir: Path
    source_md: Path


@dataclass
class InstallResult:
    slug: str
    harness: str
    target: Path | None
    action: str  # installed | replaced | migrated | skipped_conflict | skipped_no_dir | uninstalled | not_installed | missing
    reason: str = ""


def _parse_description(text: str) -> str:
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return ""
    for line in m.group(1).splitlines():
        m2 = _KV_RE.match(line.strip())
        if m2 and m2.group(1).strip().lower() == "description":
            return m2.group(2).strip().strip('"').strip("'")
    return ""


def bundle_skills(project_key: str) -> list[BundleSkill]:
    """Enumerate curated skills in a project's bundle. Empty list if the
    project has no bundle or no skills dir yet."""
    skills_root = BUNDLES_DIR / project_key / "skills"
    if not skills_root.exists() or not skills_root.is_dir():
        return []
    out: list[BundleSkill] = []
    for skill_dir in sorted(skills_root.iterdir()):
        if not skill_dir.is_dir():
            continue
        md = skill_dir / "SKILL.md"
        if not md.exists():
            continue
        try:
            text = md.read_text(encoding="utf-8")
        except OSError:
            text = ""
        out.append(
            BundleSkill(
                slug=skill_dir.name,
                name=skill_dir.name,
                description=_parse_description(text),
                skill_dir=skill_dir,
                source_md=md,
            )
        )
    return out


def harness_skill_dir(harness: str) -> Path | None:
    """The global discovery dir for a harness (back-compat alias for the
    global-scope base). Returns None for harnesses without a flat skill dir."""
    return HARNESS_SKILL_DIRS.get(harness)


def _project_repo(project_key: str) -> Path | None:
    """Resolve a tracked project's source repo to an existing on-disk path, or
    None. Used to target a repo-local skill dir for project-scoped installs."""
    if not project_key:
        return None
    try:
        from watchmen.util import tracked_source_repo
        repo = tracked_source_repo(project_key)
    except Exception:
        repo = None
    if not repo:
        return None
    p = Path(repo).expanduser()
    return p if p.exists() else None


def skill_base(
    harness: str,
    *,
    scope: str = "project",
    repo: Path | None = None,
    project_key: str = "",
) -> Path | None:
    """The discovery dir to install into, by scope.

    - ``global`` → the shared per-harness dir (routes through
      ``HARNESS_SKILL_DIRS`` so tests can redirect it and it never escapes a
      sandbox).
    - ``project`` → ``<source_repo>/<harness-subdir>/skills``. ``repo`` may be
      passed directly (callers that already resolved it, and tests); otherwise
      it's resolved from ``project_key``.

    Returns None when the harness has no flat skill dir, or when a project
    scope can't resolve a repo (caller turns that into ``skipped_no_dir``)."""
    if scope == "global":
        return HARNESS_SKILL_DIRS.get(harness)
    subdir = _HARNESS_SUBDIR.get(harness)
    if subdir is None:
        return None
    r = repo if repo is not None else _project_repo(project_key)
    if r is None:
        return None
    return Path(r) / subdir / "skills"


# ── Manifest: the record of every link watchmen created ────────────────────

def _load_manifest() -> list[dict]:
    if not MANIFEST_PATH.exists():
        return []
    try:
        data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    links = data.get("links") if isinstance(data, dict) else None
    return links if isinstance(links, list) else []


def _save_manifest(links: list[dict]) -> None:
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = MANIFEST_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"links": links}, indent=2), encoding="utf-8")
    tmp.replace(MANIFEST_PATH)


def _manifest_entry(links: list[dict], target: Path) -> dict | None:
    target_s = str(target)
    for link in links:
        if link.get("target") == target_s:
            return link
    return None


def _is_managed(target: Path, links: list[dict]) -> bool:
    r"""True only if the LIVE object at ``target`` is a symlink resolving inside
    BUNDLES_DIR — i.e. watchmen still owns it right now.

    Ownership is decided from disk, NOT from a manifest entry: a stale manifest
    row can outlive the link (the user may have deleted watchmen's symlink and
    put their own real directory there). Trusting the manifest alone would let
    uninstall/migrate ``rmtree`` that user content. The symlink-into-BUNDLES_DIR
    check is both sufficient and safe, and it already survives manifest loss.
    ``links`` is kept for signature stability (callers pass the loaded manifest).

    Uses ``target.resolve()`` to follow the symlink rather than ``readlink()``:
    on Windows ``readlink()`` can return a ``\\?\``-prefixed path that no longer
    compares equal to ``BUNDLES_DIR.resolve()``, so the membership check would
    wrongly miss. ``resolve()`` normalizes both sides the same way."""
    del links  # ownership is a live-disk fact, not a manifest claim
    if not target.is_symlink():
        return False
    try:
        target.resolve().relative_to(BUNDLES_DIR.resolve())
        return True
    except (ValueError, OSError):
        return False


def _record(links: list[dict], *, slug: str, harness: str, target: Path,
            source: Path, project_key: str, scope: str = "project") -> list[dict]:
    # Mutate in place so a `links` list shared across install_project's calls
    # accumulates every entry before the single save.
    target_s = str(target)
    links[:] = [link for link in links if link.get("target") != target_s]
    links.append({
        "slug": slug,
        "harness": harness,
        "target": target_s,
        "source": str(source),
        "project_key": project_key,
        "scope": scope,
        "installed_at": datetime.now(timezone.utc).isoformat(),
    })
    return links


def _forget(links: list[dict], target: Path) -> list[dict]:
    target_s = str(target)
    links[:] = [link for link in links if link.get("target") != target_s]
    return links


# ── Install / uninstall ────────────────────────────────────────────────────

def install_skill(
    skill: BundleSkill,
    harness: str,
    *,
    project_key: str = "",
    scope: str = "global",
    repo: Path | None = None,
    force: bool = False,
    _links: list[dict] | None = None,
    _persist: bool = True,
) -> InstallResult:
    """Symlink one bundle skill into a harness discovery dir.

    ``scope`` picks the target dir: ``global`` (the shared per-harness dir) or
    ``project`` (``<repo>/<harness-subdir>/skills``, resolved from ``repo`` or
    ``project_key``). The low-level primitive defaults to ``global``; the
    project-level orchestrators (``install_project``, the curator's
    auto-install, the CLI) default to ``project`` so curated skills land in
    their origin repo and don't leak into every other repo (#125).

    Returns an InstallResult describing what happened. The conflict policy:
    replace a watchmen-managed target, skip a user-made one unless ``force``.
    """
    base = skill_base(harness, scope=scope, repo=repo, project_key=project_key)
    if base is None:
        reason = (f"no skill dir defined for harness '{harness}'"
                  if harness not in _HARNESS_SUBDIR
                  else f"project scope: no resolvable repo for project '{project_key}'")
        return InstallResult(skill.slug, harness, None, "skipped_no_dir", reason)

    links = _load_manifest() if _links is None else _links
    target = base / skill.slug

    exists = target.exists() or target.is_symlink()
    replaced = False
    if exists:
        if _is_managed(target, links):
            replaced = True
        elif not force:
            return InstallResult(skill.slug, harness, target, "skipped_conflict",
                                 "target exists and was not created by watchmen")
        _remove_path(target)

    base.mkdir(parents=True, exist_ok=True)
    target.symlink_to(skill.skill_dir.resolve(), target_is_directory=True)
    links = _record(links, slug=skill.slug, harness=harness, target=target,
                    source=skill.skill_dir, project_key=project_key, scope=scope)
    if _persist:
        _save_manifest(links)
    return InstallResult(skill.slug, harness, target,
                         "replaced" if replaced else "installed")


def install_project(
    project_key: str,
    *,
    harnesses: list[str] | None = None,
    slugs: list[str] | None = None,
    scope: str = "project",
    force: bool = False,
) -> list[InstallResult]:
    """Install all (or a slug-filtered subset of) a project's bundle skills
    into the given harnesses (defaults to every known harness dir).

    Defaults to ``scope="project"`` — a project's skills install into its own
    repo's skill dir, not the global one. The repo is resolved once and reused
    across every skill/harness."""
    targets = harnesses if harnesses is not None else list(HARNESS_SKILL_DIRS)
    wanted = set(slugs) if slugs is not None else None
    skills = [s for s in bundle_skills(project_key)
              if wanted is None or s.slug in wanted]

    repo = _project_repo(project_key) if scope == "project" else None
    links = _load_manifest()
    results: list[InstallResult] = []
    for skill in skills:
        for harness in targets:
            results.append(install_skill(
                skill, harness, project_key=project_key, scope=scope, repo=repo,
                force=force, _links=links, _persist=False,
            ))
    _save_manifest(links)
    return results


def uninstall_skill(slug: str, harness: str, *, project_key: str | None = None) -> InstallResult:
    """Remove a watchmen-installed link. Never removes a target watchmen
    doesn't own.

    Manifest-driven so it finds the link wherever it lives (global dir or a
    repo-local one), which the old fixed-path lookup couldn't. ``project_key``
    disambiguates when the same slug is installed for more than one project.
    Falls back to the legacy global-path check when nothing is in the manifest
    (covers user-made dirs and the not-installed case)."""
    links = _load_manifest()
    matches = [
        link for link in links
        if link.get("slug") == slug and link.get("harness") == harness
        and (project_key is None or link.get("project_key") == project_key)
    ]
    if matches:
        removed_any = False
        conflict = False
        last_target = Path(matches[-1]["target"])
        for link in matches:
            target = Path(link["target"])
            present = target.exists() or target.is_symlink()
            if present and not _is_managed(target, links):
                # The row is stale: the user replaced our link with their own
                # content. Leave it untouched and keep the row so status stays
                # honest — never delete what we no longer own.
                conflict = True
                continue
            if present:
                _remove_path(target)
                removed_any = True
            _forget(links, target)  # remove our link, or clean a vanished one
        _save_manifest(links)
        if removed_any:
            return InstallResult(slug, harness, last_target, "uninstalled")
        if conflict:
            return InstallResult(slug, harness, last_target, "skipped_conflict",
                                 "target exists and was not created by watchmen")
        return InstallResult(slug, harness, last_target, "not_installed")

    # No manifest entry — fall back to the global dir to honour user-made and
    # not-installed semantics.
    base = harness_skill_dir(harness)
    if base is None:
        return InstallResult(slug, harness, None, "skipped_no_dir",
                             f"no skill dir defined for harness '{harness}'")
    target = base / slug
    if not (target.exists() or target.is_symlink()):
        return InstallResult(slug, harness, target, "not_installed")
    if not _is_managed(target, links):
        return InstallResult(slug, harness, target, "skipped_conflict",
                             "target was not created by watchmen")
    _remove_path(target)
    _save_manifest(_forget(links, target))
    return InstallResult(slug, harness, target, "uninstalled")


def _global_migration_candidates(links: list[dict]) -> dict[str, tuple[str, Path, str, str, Path]]:
    """(harness, old_target, project_key, slug, source) for every global
    watchmen link to relocate, keyed by target path to dedup.

    Two sources, because the manifest can be incomplete (manifest loss leaves
    real symlinks unrecorded):
      1. manifest entries that aren't already project-scoped and sit in a
         global dir;
      2. a filesystem sweep of each global dir for symlinks resolving into
         BUNDLES_DIR — project_key/slug inferred from the
         ``<key>/skills/<slug>`` target path.
    """
    cands: dict[str, tuple[str, Path, str, str, Path]] = {}
    for link in links:
        if link.get("scope") == "project":
            continue
        harness = link.get("harness", "")
        gbase = skill_base(harness, scope="global")
        old = Path(link.get("target", ""))
        if gbase is None or old.parent != Path(gbase):
            continue
        cands[str(old)] = (harness, old, link.get("project_key") or "",
                           link.get("slug", ""), Path(link.get("source", "")))

    bundles_root = BUNDLES_DIR.resolve()
    for harness in HARNESS_SKILL_DIRS:
        gbase = skill_base(harness, scope="global")
        if gbase is None or not gbase.exists():
            continue
        for entry in sorted(gbase.iterdir()):
            if str(entry) in cands or not entry.is_symlink():
                continue
            try:
                resolved = entry.resolve()
                rel = resolved.relative_to(bundles_root)
            except (OSError, ValueError):
                continue  # not one of ours
            parts = rel.parts
            # Exactly <project_key>/skills/<slug> — not a symlink to some
            # sub-path inside a skill (which would mis-infer slug + source).
            if len(parts) != 3 or parts[1] != "skills":
                continue
            cands[str(entry)] = (harness, entry, parts[0], parts[2], resolved)
    return cands


def migrate_to_project_scope() -> list[InstallResult]:
    """Move existing global watchmen-managed links into their origin repos.

    For each global link whose project has a resolvable repo, install a
    repo-local copy and drop the global one. Links whose repo can't be resolved
    are left global (reported, not dropped). User-made dirs are never touched.
    Idempotent: already-project-scoped links are skipped. Robust to manifest
    loss — also sweeps the global dirs on disk, not just the manifest."""
    links = _load_manifest()
    results: list[InstallResult] = []
    for harness, old_target, project_key, slug, source in _global_migration_candidates(links).values():
        repo = _project_repo(project_key)
        if repo is None:
            results.append(InstallResult(slug, harness, old_target, "skipped_no_dir",
                                         f"no repo for '{project_key}'; left global"))
            continue
        if not source.exists():
            continue  # bundle gone; leave the stale link for uninstall to clean
        skill = BundleSkill(slug, slug, "", source, source / "SKILL.md")
        res = install_skill(skill, harness, project_key=project_key, scope="project",
                            repo=repo, _links=links, _persist=False)
        if res.action in ("installed", "replaced"):
            if (old_target.exists() or old_target.is_symlink()) and _is_managed(old_target, links):
                _remove_path(old_target)
            _forget(links, old_target)
            results.append(InstallResult(slug, harness, res.target, "migrated"))
        else:
            results.append(res)
    _save_manifest(links)
    return results


def installed_targets(project_key: str | None = None) -> list[dict]:
    """Manifest entries, optionally filtered to one project. Used by the viewer
    and CLI to show install status."""
    links = _load_manifest()
    if project_key is None:
        return links
    return [link for link in links if link.get("project_key") == project_key]


def _remove_path(target: Path) -> None:
    """Unlink a symlink or remove a directory/file target."""
    if target.is_symlink() or target.is_file():
        target.unlink()
    elif target.is_dir():
        import shutil
        shutil.rmtree(target)
