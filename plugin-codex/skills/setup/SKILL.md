---
name: setup
description: Set up watchmen end-to-end for the current machine — install the CLI, run the init wizard, wire the plugin into Codex (and Claude Code), and verify the install with doctor. Use when someone wants to install, onboard, or configure watchmen with their coding agent.
---

# Set up watchmen

You are guiding the user through installing and wiring watchmen on this machine. watchmen is a local CLI that turns past coding-agent sessions into a curated skills library and surfaces it back into new sessions. This skill is a thin orchestrator: you run the non-interactive shell steps yourself, and for the steps that are interactive (the `init` wizard) or that only the agent UI can do (plugin install), you give the user the exact commands and wait.

Do not touch the user's API keys or credentials directly — the `init` wizard handles provider auth. Never paste a key on the user's behalf.

## Before you start

Check what's already here so you don't redo work:

!`uname -s; command -v uv >/dev/null && echo "uv: $(uv --version)" || echo "uv: MISSING"; command -v watchmen >/dev/null && echo "watchmen: $(watchmen --version 2>/dev/null)" || echo "watchmen: not installed"`

Read that output and branch:

- **`uv: MISSING`** → stop and tell the user to install uv first (`curl -LsSf https://astral.sh/uv/install.sh | sh`, or `brew install uv`), then re-run this skill. uv is the only hard prerequisite besides a provider credential.
- **watchmen already installed** → skip to step 3 (verify) and only fix what `doctor` flags. Don't reinstall over a working setup.
- **Fresh machine** → walk steps 1 → 4 in order.

## 1. Install the CLI

watchmen installs as an editable `uv` tool from a clone, so `git pull` + the daemon pick up updates:

```bash
git clone https://github.com/firstbatchxyz/watchmen.git
cd watchmen
uv sync && uv tool install --editable .
```

Run these for the user (pick a sensible parent dir, or ask where they want the clone). If the clone already exists, `cd` into it and just run the `uv sync && uv tool install --editable .` line.

## 2. Run the init wizard (interactive — hand off)

`watchmen init` is an interactive wizard: it asks which LLM provider to use (OpenRouter / OpenAI / Anthropic, or an OAuth subscription on macOS), prompts for that provider's API key, ingests `~/.claude/projects/` history, lets the user pick projects, previews cost, runs analyze + curate, and installs the daemon + viewer for autostart.

You can't drive an interactive prompt cleanly, so **tell the user to run it themselves** in their terminal:

```bash
watchmen init
```

Wait for them to confirm it finished before continuing. If they'd rather skip the wizard and just install the background services with defaults, `watchmen up` is the non-interactive equivalent (no provider/project picking) — but they'll still need a provider credential set via `watchmen settings api-key` or an OAuth login.

## 3. Wire the plugin into the agent (agent UI — hand off)

The plugin (skill suggestions + the `brief` digest) installs through the agent's own plugin system, which you cannot invoke from here. Give the user the exact commands for whichever agent(s) they use and ask them to paste them.

**Codex:**

```
/plugins marketplace add github:firstbatchxyz/watchmen
/plugins install watchmen
```

You then get `/skills brief` (or `$brief`) inside Codex with the workspace digest. Codex has no statusline, so the skill-suggestion hint is on-demand `brief` instead of a live indicator.

**Claude Code:**

```
/plugin marketplace add firstbatchxyz/watchmen
/plugin install watchmen@watchmen
/reload-plugins
```

Then the statusLine, one-time (Claude Code only): `watchmen statusline install`.

If watchmen was already installed and you're updating it, the refresh path is `/plugins marketplace update watchmen` then reinstall — the plugin only re-caches when its version changes.

## 4. Verify

Run doctor and read it back to the user:

!`watchmen doctor 2>&1 | tail -20`

Walk the ✓/✗/! lines. Common follow-ups:

- **viewer not responding** → `watchmen viewer install`
- **hooks not wired** → `watchmen hooks install`
- **API key invalid/missing** → `watchmen settings provider` to check, `watchmen settings api-key --provider <p>` to fix
- **no corpus / 0 sessions** → they haven't run `watchmen init` (or `watchmen ingest`) yet

Close with a one-line "you're set up" summary and point them at `watchmen status` for the daily view and `watchmen --help` for the full command surface. Keep the whole interaction tight; only dig into a step if it failed.
