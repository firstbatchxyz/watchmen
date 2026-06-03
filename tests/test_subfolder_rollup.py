"""Subfolder rollup for `repo_dir_sql_predicate`.

A tracked project should pick up sessions opened from a SUBFOLDER of its repo,
and must not match sibling dirs that merely share a prefix. The predicate is
pure SQL string logic over `project_dir` values, which are stored host-native
(POSIX `/`, Windows `\\`). We build paths with `os.sep` so the test asserts the
same behaviour on either OS, and separately prove the predicate tolerates the
*other* separator right after the prefix (mixed-source data, e.g. Codex cwds).
"""

from __future__ import annotations

import os
import sqlite3

from watchmen.paths import repo_dir_sql_predicate

SEP = os.sep
OTHER = "/" if SEP == "\\" else "\\"


def _matching(source_repo: str, project_dirs: list[str]) -> set[str]:
    where, params = repo_dir_sql_predicate(source_repo, alias="s")
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE sessions (project_dir TEXT)")
    con.executemany("INSERT INTO sessions VALUES (?)", [(d,) for d in project_dirs])
    rows = con.execute(
        f"SELECT project_dir FROM sessions s WHERE {where}", params
    ).fetchall()
    con.close()
    return {r[0] for r in rows}


def _join(*parts: str) -> str:
    return SEP + SEP.join(parts)


def test_root_matches_self_and_subfolders():
    root = _join("home", "u", "dria_augmentator_frontend")
    dirs = [
        root,                                  # the repo root itself
        root + SEP + _sub("repos", "kai-frontend"),
        root + SEP + "src",                    # another subfolder
        _join("home", "u", "other-project"),   # unrelated
    ]
    assert _matching(root, dirs) == {root, root + SEP + _sub("repos", "kai-frontend"), root + SEP + "src"}


def _sub(*parts: str) -> str:
    return SEP.join(parts)


def test_other_separator_after_prefix_still_matches():
    """Robust to mixed-source data: a child path that uses the opposite
    separator right after the repo prefix still rolls up."""
    root = _join("home", "u", "proj")
    child = root + OTHER + "sub"
    assert _matching(root, [child]) == {child}


def test_sibling_prefix_is_not_matched():
    """`…/proj` must not match `…/proj_old` — the char after the prefix must be
    a separator. Also guards against LIKE-wildcard regressions: an underscore in
    the repo name is a literal, not a single-char wildcard."""
    root = _join("a", "dria_augmentator_frontend")
    dirs = [
        root,
        _join("a", "dria_augmentator_frontend_old"),  # sibling sharing the prefix
        _join("a", "driaXaugmentatorXfrontend"),       # would match if `_` were a wildcard
    ]
    assert _matching(root, dirs) == {root}


def test_trailing_separator_in_root_is_ignored():
    root = _join("home", "u", "proj") + SEP
    base = _join("home", "u", "proj")
    assert _matching(root, [base, base + SEP + "sub"]) == {base, base + SEP + "sub"}
