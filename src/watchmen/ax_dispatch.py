"""Experimental: turn a cross-runtime route decision into a real delegation
via AX (google/ax) instead of only advising the user.

A ``switch-harness`` winner belongs to a different runtime, so watchmen can't
emit a native artifact the source harness can execute (see the advisory path in
``route_rewrite``). When AX is configured — a running ``ax serve`` with wrappers
for the target harness — we instead emit a dispatcher that shells out to
``ax exec --agent <target>``, so the source agent delegates the skill to the
winning harness for real. When AX is not configured, callers fall back to the
advisory line, so default behavior is unchanged.

Gating is env-only, opt-in:

  WATCHMEN_AX_SERVER  gRPC address of a running ``ax serve`` (e.g.
                      ``localhost:8494``). Unset disables watchmen's AX dispatch
                      (the advisory fallback). NB: ``ax exec`` with an empty
                      ``--server`` actually spins a *local* built-in server off
                      ``ax.yaml`` — so "unset" is watchmen's gate, not AX's.
  WATCHMEN_AX_BIN     path to the ``ax`` binary (default: ``ax`` on PATH).

AX CLI state (verified against google/ax @ 2026-06-09):
  - ``ax exec`` flags: ``--agent --server --input --conversation --resume
    --last-seq --config``. The old ``--once`` is GONE (see ax_exec_command).
  - ``ax serve`` runs the controller as a gRPC server (address from ax.yaml).
  - ``ax fork`` (``--src-conversation/--src-seq/--dest-conversation``) forks an
    event log from a checkpoint — the native primitive for the fork-and-race
    delegation trigger (#96); watchmen wouldn't need to hand-roll it.
  - Faithful cross-execution resume is still gated by google/ax#19
    (``internal_only`` messages not replayed) — OPEN as of this date. So the
    "headless now, AX-native later" call (#96) stands; this dispatch stays
    experimental and unverified end-to-end until a local ``ax`` is wired up.
"""

from __future__ import annotations

import os

# watchmen harness slug -> AX agent id as registered in the running ``ax serve``.
# Only harnesses with an AX wrapper can be dispatch *targets*; anything not
# listed falls back to the advisory path.
HARNESS_TO_AX_AGENT = {
    "claude_code": "claude-code",
    "codex": "codex",
}


def ax_server() -> str | None:
    """gRPC address of the running ``ax serve``, or None when AX dispatch is off."""
    return os.environ.get("WATCHMEN_AX_SERVER") or None


def ax_bin() -> str:
    """Path to the ``ax`` binary (``ax`` on PATH by default)."""
    return os.environ.get("WATCHMEN_AX_BIN", "ax")


def ax_agent_for(harness: str | None) -> str | None:
    """The AX agent id that can run ``harness``'s runtime, or None when AX is
    unconfigured or no wrapper exists for it. This is the availability gate the
    rewriter checks before choosing AX dispatch over the advisory fallback."""
    if not harness or ax_server() is None:
        return None
    return HARNESS_TO_AX_AGENT.get(harness)


def ax_exec_command(*, agent: str, model: str | None, workspace: str | None) -> str:
    """Build the ``ax exec`` invocation a dispatcher subagent should run.

    ``ax exec`` has no ``--workspace`` / ``--model`` flags, so both ride the
    wrapper's ``[workspace]`` / ``[model]`` header convention inside ``--input``.
    The concrete task isn't known until dispatch time, so the returned command
    carries a literal ``<TASK>`` placeholder the subagent replaces with the
    request it was handed.
    """
    headers: list[str] = []
    if model:
        headers.append(f"[model] {model}")
    if workspace:
        headers.append(f"[workspace] {workspace}")
    header_block = ("\n".join(headers) + "\n\n") if headers else ""
    # Headless single-shot. `ax exec` is a REPL: it runs the turn seeded by
    # `--input`, then loops and prompts for the next message (cmd/ax/exec.go
    # execLoop → promptUser, verified against google/ax @ 2026-06-09). There is
    # NO `--once` flag anymore — the spike used to pass it and current AX would
    # reject it. Redirecting stdin from /dev/null makes that post-turn prompt
    # hit EOF and stop instead of hanging a non-TTY dispatcher shell, so the
    # call bounds to the single seeded turn (turn 1 never prompts — `--input`
    # is non-empty). NOTE: the exact exit-code on EOF (vs a clean `q`) is
    # unverified pending a local `ax` build; this is the experiment's known gap.
    # AX has no `--workspace` / `--model` flags, so both ride the wrapper's
    # `[workspace]` / `[model]` header convention inside `--input`.
    #
    # Single-quoted input preserves newlines and spares the subagent from
    # escaping the task body. (A task containing a single quote would break the
    # quoting — acceptable for the experiment; a wrapper script is the hardening
    # path.)
    return (
        f"{ax_bin()} exec --server {ax_server()} --agent {agent} "
        f"--input '{header_block}<TASK>' < /dev/null"
    )
