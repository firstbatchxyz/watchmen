# Changelog

All notable changes to watchmen are listed here. The CLI surfaces the latest
release notes once per version bump (CLI + web viewer) so a `git pull` is
never silent. Format loosely follows [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

### Fixed — Codex subagent telemetry
- Before this change the codex adapter always set `is_subagent=0`, so
  every codex session counted toward the user-facing top-level total
  even when codex had spawned it as a thread-subagent. The dashboard
  subagent KPI card and `watchmen subagents` cost-share reported 0% for
  codex projects regardless of actual subagent activity.
- Codex 0.133.0 exposes session lineage via `session_meta.source`. The
  adapter now reads that field: `thread_spawn` subagents get
  `is_subagent=1` with `parent_session_id` filled from the parent thread,
  while internal codex bookkeeping subagents (Review, Compact,
  MemoryConsolidation) stay `is_subagent=0` so they do not inflate
  user-facing delegation metrics.
- Pre-0.133.0 rollouts have no lineage data on disk; those sessions
  still report `is_subagent=0` like before.

### Added — Skill mesh distillation planner
- New `watchmen distill <project>` inspects the project's created skills,
  runs pairwise LLM comparisons with a structured similarity rubric, and writes
  `bundles/<project>/_distill_plan.json` with semantic judgments,
  differences, merge clusters, and a context-rot score.
- Semantic distillation uses the globally configured default model. For
  strict JSON-rubric consistency you can point distill at a stronger model
  via `WATCHMEN_DISTILL_MODEL` or `--model`.
- Similarity now defaults to low-noise skill metadata, with
  `--scope skill-md` and `--scope folder` for broader matching when wanted.
- `watchmen distill <project> --local` skips the LLM judge and shows only the
  offline candidate mesh.
- `watchmen distill <project> --stage` creates merged draft skills under
  `bundles/<project>/_pending/` so the existing `watchmen review` gate can
  approve them before any original skills are dropped.
- After a semantic distill run in a TTY, watchmen can open an arrow-key merge
  picker with live candidate summaries. Space toggles drafts, Enter promotes
  selected distilled skills, and superseded originals are archived under
  `_distilled_archive/` while their slugs are blocklisted.
- `watchmen distill <project> --animate` renders a Rich terminal mesh view:
  skills connect, clusters form, and overlapping bundles reduce into fewer
  staged candidates.

## [0.6.6] — 2026-05-20

Mission-control gets a fourth KPI card so the subagent signal sits next
to the existing skill/error/repos cards instead of being hidden behind
the per-project drill-down.

### Added — Subagent sessions KPI card on the dashboard
- New "Subagent sessions · 7d" card alongside the existing three.
  Headline value is the subagent session count this week with the
  usual prior-week comparison and color-coded delta arrow.
- Subtitle carries the current-window **cost share** ("X% of cost"),
  because the count alone can swing wildly between weeks — a project
  with two long monolithic main sessions and twenty small subagent
  sessions looks delegation-heavy by count but isn't by spend.
- Grid layout adapts: 4 columns on desktop, 2 on tablets, 1 on phones.
- `impact_strip()` now returns a `subagent_sessions` key with the
  same `{this, prior, delta_pct, fmt}` shape as the other KPIs, plus
  a `cost_share_pct` rider for the subtitle.

## [0.6.5] — 2026-05-20

First step toward the subagent-cost-efficiency thread: surface what the
corpus already knows about subagent usage so the gap between projects
that delegate heavily and projects that don't is visible. Measurement
first, intervention next.

### Added — Subagent usage surface
- New `watchmen subagents` command shows subagent cost share per agent
  and per project, with a per-project detail view (`--project <key>`)
  that lists the top main-thread sessions ranked by cost as
  "delegation candidates" — the sessions that probably should have
  delegated routine search/read work to subagents to keep the parent's
  context window smaller.
- New section on the per-project web viewer page (`/p/<project_key>`)
  mirrors the CLI: main vs. subagent cost split, share badge color-coded
  by intensity (red / amber / green), and the same delegation-candidate
  table. Renders nothing when the project has no captured sessions yet.
- All four adapters have populated `sessions.is_subagent` since the
  adapter rewrite, but Claude Code is the only one with a transcript-
  level subagent primitive today. Codex / pi.dev / OpenCode rows always
  report `is_subagent=0` until upstream adds a primitive — the surface
  is honest about that gap rather than papering over it with heuristics.

## [0.6.4] — 2026-05-19

Adapter coverage release. Adds the OpenCode adapter and fixes a real gap
in cross-adapter skill telemetry that had been hiding in plain sight:
only Claude Code transcripts were producing per-skill usage counts, so
`watchmen prune` and the dashboard sparklines were blind to skill
activations in Codex, pi.dev, and OpenCode sessions.

### Added — OpenCode Adapter
- New `opencode` adapter in `src/watchmen/adapters/opencode.py`.
- Supports ingesting sessions exported from the OpenCode CLI (`opencode export`).
- Auto-discovery of session JSON files in `~/.opencode/sessions/` and `~/.local/share/opencode/sessions/`.
- Extracts user prompts, assistant text, internal reasoning (Chain of Thought), and tool execution history (including errors).
- Full token-usage and cost attribution based on the exported model metadata.

### Fixed — Cross-adapter skill attribution
- New `adapters/_shared.py` ships an `extract_skill_from_path()` helper
  that scans tool-call arguments for a `…/skills/<slug>/SKILL.md` path
  and returns the slug. Wired into the Codex, pi.dev, and OpenCode
  adapters so every read of a SKILL.md file populates the `skill_name`
  column the same way Claude Code's `Skill` tool already does.
- Effect: `watchmen prune`'s usage telemetry now sees skill activations
  across all four supported agents, not just Claude Code. The dashboard
  sparklines and the prune judge's per-skill `usage_count`/`last_fired_at`
  numbers reflect real usage regardless of which agent invoked the skill.

## [0.6.3] — 2026-05-19

Hotfix for the false-success scenario in `watchmen up` discovered during
v0.6.2 testing — `install_viewer` could return 0 while launchctl had
silently no-op'd the load, leaving "✓ watchmen is up" lying about the
actual state.

### Fixed — `watchmen up` no longer falsely declares success
- `launchd_setup._bootstrap` now verifies the load actually took by
  calling `launchctl list <label>` after `launchctl bootstrap`. The
  race between the prior `bootout` (asynchronous from launchd's
  perspective) and the new `bootstrap` could leave bootstrap reporting
  success with no live unit; we now retry once with a 500ms sleep when
  the verify fails, which is enough on every macOS I've tested.
- `watchmen up` does its own post-install verify via
  `service.is_daemon_loaded()` / `is_viewer_loaded()` — belt-and-
  suspenders, so a future race in any backend gets surfaced honestly
  instead of swallowed into the success summary.

## [0.6.2] — 2026-05-19

Daemon lifecycle simplification. Three quick wins that turn the
multi-step "daemon install + viewer install + hooks install + remember
to reinstall after settings change" routine into a couple of one-liners.

### Added — `watchmen up` and `watchmen down`
- `watchmen up` installs daemon + viewer + hooks in one shot, auto-starts
  the services, and prints a success summary with the viewer URL,
  daemon interval, and a provider-banner line so it's clear which
  endpoint will handle the runs. `--skip-{hooks,daemon,viewer}` opts
  out of one subsystem (CI, remote servers, etc.).
- `watchmen down` is the inverse — uninstalls the scheduler units +
  hooks settings entries with a single confirmation prompt. Does NOT
  touch `corpus.db`, `state.db`, or `bundles/`; running `watchmen up`
  later brings everything back to where it was.

### Changed — Unified `watchmen status`
- The four previously separate views (`daemon status`, `viewer status`,
  `hooks status`, `doctor`) collapsed into one screen. New top section
  shows: active provider banner, services row (daemon / viewer / hooks
  with ✓/· per host), corpus health (session count, latest transcript
  time, last-7d skill calls). Below stays the project queue + recent
  runs table you already had.
- New `hooks_setup.is_installed_summary()` helper powers the hooks row
  without printing — uses the existing basename-matcher so stale
  absolute paths from earlier installs still register as "installed".

### Added — Auto-prompt reinstall on settings change
- Switching provider or default model previously left the daemon's
  scheduler unit baking the old config until you remembered to
  `watchmen daemon install`. The four entry points
  (`settings provider`, `settings model`, interactive menu provider
  switch, interactive menu model edit) now call
  `service.notify_settings_changed()` which detects an installed daemon
  and offers an in-place reinstall — no more silent staleness. The
  viewer settings page surfaces the same warning in its flash banner.

## [0.6.1] — 2026-05-19

This release ships **`watchmen prune`** — the cleaner mode promised at
launch. The curator is deliberately greedy when it bundles skills (better
to over-generate than to under-cover); prune is the counterweight.

### Added — `watchmen prune <project>` (LLM-judge skill review)
- New `watchmen prune <project>` runs an LLM-judge over the project's
  bundled skills + workspace brief (CLAUDE.md / AGENTS.md) + per-skill
  usage telemetry from `corpus.db`. The judge is an agent with tools:
  - `read_skill_full(slug)` — pull the full SKILL.md body
  - `read_transcript_excerpts(skill_name)` — session windows where the
    skill actually fired, so the judge can verify usage matches the
    skill's stated trigger phrases
  - `read_repo_file` / `list_repo_files` — verify the skill still
    matches the current source repo
  - `flag_skill(slug, severity, reason)` — push onto the review queue
- Writes `bundles/<project>/_prune_queue.json` — no auto-deletes.
- **Aggressive mode by default** — flag anything that looks low-value
  (never-fired skills, contradictions with siblings, redundancy with
  the workspace brief, drifted references to deleted code, vague
  triggers). Human reviews every flag in the UI.
- `watchmen prune --all` iterates every tracked project sequentially.
- `watchmen prune <project> --apply` consumes the queue interactively
  in the terminal (k/d/s/q prompts).
- `--model` override matches the curator/analyst conventions; default
  resolves to the active provider's pick (works on subscription OAuth
  out of the box).

### Added — Viewer review queue (`/p/<project>/prune`)
- Renders the flagged skills with severity badge + judge's reason +
  per-skill description preview + Approve (delete) / Dismiss (keep)
  buttons.
- Dismissed slugs persist in `_prune_dismissed.json` and surface back to
  the judge on the next run — explicit ("I was kept previously, re-flag
  only with new evidence") rather than silent suppression.
- "Prune skills →" link added to the project page header.

### Added — Per-skill usage telemetry in corpus.db
- New `tool_calls.skill_name` column — captures the `input.skill =
  '<slug>'` payload from Claude Code's `Skill` tool_use blocks. Drives
  the prune signal but also available for any future analytics. Backed
  by an idempotent migration so existing corpus.db users only need
  `watchmen ingest --full` once to backfill.
- `watchmen ingest --full` flag — surfaces the existing `scan_all(full=True)`
  path on the CLI. Needed for the one-shot skill_name backfill on
  pre-0.6.1 installs.

## [0.6.0] — 2026-05-19

This release lifts watchmen off the OpenRouter-only assumption it shipped
with in 0.5: multi-provider API-key auth (OpenAI / Anthropic / OpenRouter),
OAuth credential reuse for Claude Pro and ChatGPT subscriptions, an
interactive `watchmen settings` menu, `watchmen reset <project>` for
from-scratch re-curates, an impact-card treatment-date bugfix, and a
provider startup banner so it's always obvious which endpoint + billing
mode a run is using.

### Added — Provider startup banner + billing-mode visibility
- Analyst + curator runs now print a one-line banner at startup:
  `provider=X · <quota label> · model=Y · endpoint=Z`. Removes the
  ambiguity around which credential is in flight — particularly useful
  after switching providers via `watchmen settings provider`.
- Onboarding cost-preview panel now reads the active provider: subscription
  providers (`claude-pro`, `chatgpt`) get a "no API spend — counts toward
  your rate-limit window" note instead of a dollar range. The
  deepseek-v4-flash-pricing footnote is gone — it was misleading for
  anyone not on the OpenRouter default.
- `OpenAIProvider`'s silent fallback to `~/.codex/auth.json`'s api-key
  field now emits a one-time stderr warning when fired, so users aren't
  surprised which OpenAI org is being billed.

### Added — OAuth credential reuse: Claude Pro + ChatGPT subscriptions
- **Claude Pro / Team / Max via Claude Code OAuth** — new `claude-pro`
  provider reads the OAuth token Claude Code stores in the macOS keychain
  (`Claude Code-credentials` entry) and posts to `api.anthropic.com/v1/messages`
  with the `anthropic-beta: oauth-2025-04-20` header. **Calls are billed
  against your Claude subscription quota, not per-token API credit.**
  No paste step — token rotation handled by Claude Code itself.
- **ChatGPT subscription via Codex OAuth (experimental)** — new `chatgpt`
  provider reads `~/.codex/auth.json`'s ChatGPT-account OAuth token and
  posts to the Codex Responses API at `chatgpt.com/backend-api/codex/responses`.
  Full Responses API ↔ chat-completions translator + SSE event stream
  aggregator so the agent loop stays provider-agnostic. Models: `gpt-5.5`,
  `gpt-5.4`, `gpt-5.4-mini`, `gpt-5.3-codex`, `gpt-5.2`. Marked
  experimental because the model whitelist is undocumented and may
  change without notice.
- **Codex api-key reuse** — when the `openai` provider has no
  `OPENAI_API_KEY` set in env / .env, it falls back to reading the
  `OPENAI_API_KEY` field from `~/.codex/auth.json` (api-key mode users).
  No re-pasting needed if you already configured Codex.
- New `watchmen.credentials` package with `claude_code` + `codex` readers.
  Both are macOS-aware and degrade gracefully (return None) on Linux /
  fresh installs without the upstream CLI.
- Provider abstraction extended with `resolve_api_key()` (credential
  discovery hook) and `custom_transport`/`call()` (lets streaming
  providers own their HTTP round trip). Adding a fourth OAuth-flavored
  provider is one subclass + the credential reader.
- Settings surface across CLI / interactive menu / web UI now distinguishes
  env-var-based providers (key form) from OAuth providers (read-only
  status + login hint). `watchmen doctor` probes OAuth credentials via
  local metadata (expiry, scopes) rather than burning subscription
  quota on a network probe.
- Onboard wizard auto-detects local Claude Code / Codex installations
  and surfaces the OAuth options when present, defaulting to
  `claude-pro` if available (zero-paste setup).

### Added — `watchmen reset <project>` for from-scratch re-curates
- New command wipes a project's analyses + curated bundle (CLAUDE.md,
  AGENTS.md, skills/, _candidates.json, _curation_log.md, _index.md,
  _pending/, _run.log) and resets state.db's `last_analyst_*` /
  `last_curator_*` markers — so the next `watchmen learn` (or analyst +
  curator pair) treats the project as a fresh install.
- Preserves `_pinned.json` + `_blocklist.json` by default (your steering
  intent); `--wipe-all` removes those too.
- Safety: `--dry-run` lists what would be removed without touching
  anything; the default destructive path requires typing the project key
  back to confirm; `--yes` skips that prompt for CI / scripting.
- `--then-learn` chains directly into `watchmen learn --full` after the
  reset so "wipe and rerun from scratch" is a single command. `--model`
  flag passes through to the chained learn for one-off model overrides.
- Corpus.db, raw transcripts, and the project row config (`source_repo`,
  `threshold`, `notes`, `enabled`, `approval_required`,
  `skip_overlapping_skills`) are never touched.

### Added — Interactive `watchmen settings` menu
- `watchmen settings` (no subcommand) now opens an arrow-key navigable menu
  with breadcrumb headers — pick "Provider & API key", "Default model",
  "Viewer port", or "Per-project settings", drill in, edit, and bounce back
  with Enter / Esc / a "Back" entry at every level. Built on `questionary`
  (pulls in `prompt_toolkit`, which Rich already depended on transitively).
- Per-project page exposes Enabled toggle, threshold, approval_required,
  skip_overlapping_skills, and free-text notes — same surface as
  `watchmen settings set` but discoverable.
- Falls back to a non-interactive cheatsheet when stdin/stdout aren't TTYs
  (CI, piped invocations) so the command never blocks. Flat subcommands
  remain available for scripting.

### Added — `watchmen settings model` subcommand
- `watchmen settings model`            — show current default + each provider's default
- `watchmen settings model <name>`     — persist `WATCHMEN_DEFAULT_MODEL=<name>` (lets you pin gpt-5 etc. across daemon restarts)
- `watchmen settings model --clear`    — remove the override, fall back to the active provider's default
- New `config.clear_env_var()` helper backs the clear flow; returns True/False so callers can distinguish a no-op from a real rollback.

### Added — Multi-provider auth (OpenRouter / OpenAI / Anthropic)
- watchmen no longer requires an OpenRouter account. Native API key support
  for OpenAI direct (`OPENAI_API_KEY`) and Anthropic direct (`ANTHROPIC_API_KEY`)
  alongside the existing OpenRouter path. Switch via
  `watchmen settings provider <name>`; the wizard prompts for choice on first
  run. Existing OpenRouter-only installs upgrade transparently — the auto-detect
  prefers OpenRouter when its key is present, so `watchmen <anything>` keeps
  working without re-onboarding.
- New module `watchmen.providers` houses the per-provider abstraction
  (endpoint, auth headers, request/response translator). Adding a fourth
  provider is one subclass, not a refactor of the agent loop.
- Anthropic provider includes a full **Messages API ↔ chat-completions
  translator** so the existing agent.run loop (which speaks the OpenAI
  wire format) routes through Anthropic without touching tool dispatch
  internals. System messages get lifted to the top-level `system` field,
  OpenAI-style `tool_calls` translate to Anthropic `tool_use` content blocks,
  tool result messages wrap into user messages with `tool_result` blocks,
  responses fold back into `choices[0].message.content/tool_calls` shape.
- New CLI: `watchmen settings provider [name]` (get / set the active provider
  + show per-provider key status), `watchmen settings api-key --provider <name>`
  (set or check a specific provider's key, live-validated). Bare
  `watchmen settings api-key` defaults to the active provider.
- `watchmen doctor` and the web `/doctor` panel now probe whichever provider
  is active. The viewer `/settings` JSON snapshot exposes a `providers` table
  for the UI to render per-provider key status.
- Default model is now provider-aware: `deepseek/deepseek-v4-flash` on
  OpenRouter, `gpt-5-mini` on OpenAI, `claude-haiku-4-5-20251001` on Anthropic.
  `WATCHMEN_DEFAULT_MODEL` still overrides if set.
- 22 new tests in `tests/test_providers.py` covering provider selection
  priority, the Anthropic Messages API translator (request + response, system
  field lifting, tool schema rename, content-block round-tripping), and
  end-to-end `agent.chat_call` dispatch through each provider with stubbed
  httpx.

### Fixed — plugin layout + viewer rendering bugs caught during E2E
- `_latest_digest_path()` returned the cross-agent narrative file instead
  of the newest deep-digest when both lived in `~/.watchmen/insights/` —
  reverse-alphabetical sort over `*.md` bubbled `agent_comparison.md`
  above date-prefixed digests. Now globs `[0-9]*.md` so stable-named
  cache files don't shadow timestamped runs.
- `/metrics` route silently dropped the cross-agent narrative because it
  imported `render_md` from `watchmen.view` (function lives in
  `viewer/server.py`); the bad import got swallowed by the surrounding
  `try/except`, so the narrative panel rendered empty even when the
  cache file existed. Fixed by using the local helper.
- Claude Code plugin `hooks.json` was missing the top-level
  `{"hooks": {...}}` wrapper Claude's plugin loader expects (we shipped
  the inner events dict). `/plugin install watchmen` failed with
  `expected record, received undefined`.
- Codex plugin shipped in a broken hybrid layout: `.codex-plugin/` manifest
  dir (Codex native) + `hooks/hooks.json` in a subdir (Claude-compat
  convention). Codex's loader saw the native manifest and looked for
  `hooks.json` at plugin root — found nothing — so the "Hooks" panel
  said "No plugin hooks." Rebuilt in proper native Codex layout:
  `hooks.json` at root, plus a full `interface` block in `.codex-plugin/plugin.json`
  (displayName, brandColor, category, capabilities, defaultPrompt,
  longDescription) so Codex renders the plugin as a first-class tile.
- Smoke test `test_codex_plugin_dir_has_required_layout` updated to enforce
  the native layout + presence of the required `interface` fields.

### Added — "How you use each agent" cross-agent comparison
- New section at the top of `/metrics` (under the profile card) and
  inline above the deep digest on `/insights` compares how the user
  works with each coding agent they have data for. Two pieces:
  - **Deterministic per-adapter facts table** (always rendered when
    ≥2 adapters have data): side-by-side cards with sessions, active
    days, prompts, prompts/session, tool calls/session, error rate,
    cache hit, $/prompt, $/session, total $, avg session length, top
    tools, top projects, dominant model. Pure SQL from corpus.db.
  - **LLM-synthesized narrative** (cached, rendered when present):
    a short markdown panel — "Per-agent character / Where each one wins
    / Re-balancing suggestion" — answering "which agent is best for
    which kind of work in YOUR hands". Generated as part of the
    existing `watchmen insights` digest pipeline (no extra command,
    no extra UI button); writes to `~/.watchmen/insights/agent_comparison.md`
    with YAML frontmatter for the viewer to display generation time + model.
- New `metrics.agent_comparison_facts(days)` — pure SQL helper returning
  the rich per-adapter dict that feeds both the renderer and the LLM
  prompt. Joins sessions × tool_calls for top-tool lists, sessions ×
  suggestions.jsonl for per-adapter skill-suggestion fire counts.
- New `commands/insights._cross_agent_narrative(facts, model)` — single
  LLM call with a focused prompt. Returns None (and the viewer suppresses
  the narrative panel) when fewer than 2 adapters have ≥10 prompts each,
  so a one-agent user never sees a redundant "compared to no one" panel.

### Added — Codex watchmen plugin (symmetric to the Claude Code plugin)
- New `plugin-codex/` directory ships a Codex-native plugin with the same
  shape as `plugin/`: manifest under `.codex-plugin/plugin.json`, the
  `brief` skill, the `UserPromptSubmit → check_prompt.sh` hook, and the
  shared `bin/` helpers. The `bin/` and skill scripts are byte-identical
  to the Claude Code plugin (Codex sets `${CLAUDE_PLUGIN_ROOT}` as a
  compat env var and the scripts self-locate via `$0`); a sync test in
  `tests/test_smoke.py` keeps the two trees in lockstep.
- New `.agents/plugins/marketplace.json` marketplace manifest, so Codex
  users can install with `/plugins marketplace add github:firstbatchxyz/watchmen`
  followed by `/plugins install watchmen`.
- `watchmen hooks install` now wires hooks into **both**
  `~/.claude/settings.json` (Claude Code) **and** `~/.codex/hooks.json`
  (Codex) in one shot. Codex's supported-event set (SessionStart,
  PreToolUse, PostToolUse, UserPromptSubmit, Stop) is enforced — entries
  for events Codex ignores (SessionEnd / SubagentStop / Notification /
  PreCompact) are not written to the Codex config.
- `watchmen hooks uninstall` and `watchmen hooks status` likewise cover
  both targets. Each host is skipped silently when its agent isn't
  installed (no `~/.codex/` dir → no Codex install attempted).
- The same basename-matching self-heal that landed for Claude Code now
  applies to `~/.codex/hooks.json` too — re-running install scrubs any
  stale watchmen entries (older paths, retired script names) before
  writing the canonical set fresh.

### Added — profile card at the top of `/metrics` (FM-style stats card)
- New section at the top of `http://127.0.0.1:8979/metrics` renders a
  Football-Manager-style profile card for how the user works with
  coding agents. Six spider axes:
  - **Throughput** — prompts per active day
  - **Frugality** — inverse $/prompt
  - **Reliability** — 1 − tool error rate
  - **Curiosity** — distinct tool names used
  - **Range** — distinct projects touched
  - **Mastery** — curated skill bundles on disk
- Each axis normalized 0–1 against tunable elite caps. Overall rating
  mapped to 40–99 (FIFA convention; even an empty corpus still gets a
  Newcomer card at 40). Tier gradient on the header shifts gold /
  silver / bronze / indigo with the rating.
- Three-column attribute grid (Volume / Efficiency / Breadth) with each
  stat color-coded green / yellow / red based on a normalized score —
  the FM signature. Stats include throughput, sessions, active days,
  tool calls, prompts/sess, reliability, frugality, cache hit, cost/sess,
  total spend, curiosity, range, mastery, distinct agents, top agent.
- Procedural "player traits" — short pill-shaped badges derived from
  the stats: *Codex-first*, *Multi-agent*, *Speedrunner*, *Tool collector*,
  *Multi-repo hopper*, *Reliability master*, *Curator*, *Cache wizard*,
  *Heavy spender* / *Frugal*. Capped at five so the row doesn't wrap.
- Hex spider chart with FM-style tinted concentric rings (red core →
  orange → yellow → green elite) so the user's polygon visually shows
  "distance from elite". Indigo polygon + white vertex dots on top.
- Window selector (30 / 90 / 180 / 365 / 730 days) restricts the
  corpus slice; mastery (bundle count) is always-current since curated
  skills don't age out.
- Archetype label picked from the dominant axis when one is clearly
  ahead: Speedrunner (throughput), Minimalist (frugality), Perfectionist
  (reliability), Explorer (curiosity), Polyglot (range), Curator
  (mastery). Otherwise Generalist; empty corpus → Newcomer.
- Whole section is HTML+SVG, no JS. Right-click the section or the
  spider SVG to save / screenshot for sharing.
- Mini-visualization row directly below the hero: **agent mix donut**
  (per-adapter session shares with legend), **top tools horizontal
  bars** (5 most-used tool names), **daily activity sparklines**
  (sessions / cost / tool-errors per day across the selected window).
  Each lives in its own muted tile so the whole panel reads like a
  trading-card splash page rather than a wall of numbers.

### Fixed — stale Claude Code hook entries from older watchmen installs
- `watchmen hooks install` is now self-healing: it scrubs any existing
  watchmen entry from `~/.claude/settings.json` (matched by script
  basename — `watchmen_observe.sh`, `watchmen_brief.sh`) before writing
  the canonical set fresh. Previously, both install and uninstall
  matched by exact absolute path, so when the package layout moved in
  0.1.x → 0.5.x (top-level `hooks/` → `src/watchmen/hooks/`) the old
  entries became invisible to the CLI and silently failed with "No
  such file or directory" on every PreToolUse / PostToolUse / etc.
- `watchmen hooks uninstall` shares the same basename-matching path
  now, so it fully detaches every watchmen-shipped hook regardless of
  which absolute path it was written with.
- Same fix retires the `_scrub_legacy_hooks` + `_LEGACY_HOOK_PATHS`
  surface; both got folded into the new `_scrub_watchmen_hooks` helper
  driven by `WATCHMEN_SCRIPT_NAMES` (every basename watchmen has ever
  shipped, including retired ones).

### Changed — agent-agnostic framing
- README, CLI help text, viewer copy, onboard wizard prompts, and curator LLM
  prompts no longer single out Claude Code where the behavior is multi-adapter.
  Strings that genuinely refer to data attribution (per-agent stats, hook
  installer steps that wire `~/.claude/settings.json`) keep their specific
  labels; everything else reads as "coding-agent session" / "coding-agent
  transcripts".
- `watchmen ingest` help text now reads "re-scan all coding-agent transcripts
  into corpus.db" — accurate, since the ingest path walks every adapter
  (`adapters/claude_code.py`, `adapters/codex.py`, `adapters/pi.py`), not
  just `~/.claude/projects/`.

### Added — `AGENTS.md` mirror alongside `CLAUDE.md`
- After Stage 3 of the curator writes `bundles/<project>/CLAUDE.md`, watchmen
  also writes an identical `bundles/<project>/AGENTS.md`. Codex reads
  `AGENTS.md`; Claude Code reads `CLAUDE.md`. Identical content, single source
  of truth, both agents pick up the same workspace brief without a
  per-project copy step.
- `_changelog.md` manifest tracking widened to follow `AGENTS.md` mtime
  alongside `CLAUDE.md` so regen events show up in `watchmen recent`.

### Fixed — stale `~/.watchmen/kai_claude/` directory
- The 0.5 rename `kai_claude → bundles` left some installs with both
  directories on disk (an older daemon recreated the alias after the
  migration ran, or a stale checkout copy lingered). On next import,
  `runtime_path` now archives a coexisting `kai_claude/` to
  `kai_claude.legacy/` once and prints a one-line stderr notice. Idempotent
  — the rename is self-deleting, so subsequent runs are silent.

### Added — per-coding-agent metric surfaces
- New `metrics.adapter_breakdown_all(days, tracked_only)` aggregator
  rolls up sessions, projects, prompts, tool errors, and cost per `agent`
  column in `corpus.db.sessions`. Stable order (sessions desc, agent
  alpha) so the surface is deterministic across CLI + viewer.
- Viewer's `/metrics` page gains a "By coding agent (last 30 days)"
  section: friendly labels ("Claude Code", "Codex", "pi.dev"), bar chart
  for relative session volume, columns for projects / prompts / tool
  errors / cost USD. Section hides itself on empty corpora.
- CLI's `watchmen metrics` table renamed from "Sessions by adapter" to
  "By coding agent — last Nd" and gains columns for prompts, tool errors,
  and cost USD on top of the existing sessions / share / projects.
- New `ADAPTER_LABELS` mapping + `adapter_label(slug)` helper exported
  from `metrics.py` so every UI surface can render friendly names
  consistently and unknown slugs fall through verbatim.

### Changed — viewer aesthetic upgrade (Tailwind via CDN, shadcn-inspired)
- Switched the viewer's body font from system fonts to Inter (loaded via
  Bunny Fonts, a GDPR-safe Google Fonts mirror). Sets a consistent visual
  baseline across macOS, Linux, and Windows.
- Introduced a small design-token layer in `base.html` (CSS custom
  properties for `--background` / `--foreground` / `--card` / `--border`
  / `--accent`) so future dark-mode + theme variants only need to swap
  the HSL triples.
- New utility classes: `.wm-card` (the polished card baseline), `.wm-card-table`
  (a card that hosts a flush table with no edge padding), `.wm-pill` +
  `.wm-pill-ok`/`-running`/`-fail`/`-muted` (shadcn-style status badges),
  `.wm-nav-link` (pill-style nav with active-route highlight via
  `request.url.path`). Existing `.stat-card` rules promoted to use the
  same tokens so legacy templates pick up the upgrade without rewriting.
- Templates migrated to the new primitives: dashboard, runs, insights,
  metrics_all. Per-template `<style>` blocks that duplicated the global
  card baseline got pruned; only page-specific bits stay local
  (`.daily` table on metrics, `.badge-curated`/`.badge-candidate` on
  insights, etc.).
- Nav now highlights the active section. Bigger border radius on cards
  (12px → "rounded-card") + subtler shadows + a smoother hover state
  on table rows pull the look closer to shadcn's default light theme
  without dragging in a Node build step or React.

### Changed — sharpened the Metrics vs Insights split
- `/insights` is now the LLM-driven view only: cross-repo digest,
  friction signals, untapped corpora, frustration samples, deep digest
  cache. Raw aggregations moved out.
- Removed from `/insights` (now live exclusively on `/metrics`):
  the 4 stat tiles (sessions / tool errors / frustration / cost), the
  adapter mix card, and the skill bundles count card. A top-of-page
  link `↗ aggregated metrics` points users to the raw breakdown.
- Repos table, friction signal charts, and the hour-of-day heatmap
  remain on `/insights` for LLM-context — they back the deep digest.

## [0.5.0] — 2026-05-15

First PyPI release. This rolls up everything since 0.4.0 — Linux daemon
backend, hardened internals, pytest migration, and 30 new tests filling
the biggest coverage holes. The CLI surface is unchanged; existing users
upgrade in place.

Install: `uv tool install dria-watchmen` (or `pip install dria-watchmen`).
The CLI binary is still `watchmen` and the import path is still
`from watchmen import ...` — only the package name on PyPI is
`dria-watchmen` (the plain `watchmen` namespace was already claimed).

### Added — Linux daemon backend (systemd --user)
- `watchmen daemon install` / `watchmen viewer install` now work on Linux,
  writing systemd `--user` units to `~/.config/systemd/user/` and calling
  `systemctl --user enable --now`. macOS continues to use launchd; the CLI
  is identical on either platform.
- New `watchmen.service` module dispatches to the right backend
  (`launchd_setup` on Darwin, `systemd_setup` on Linux) based on
  `platform.system()`. Status rows show "(launchd)" or "(systemd)"
  contextually. `BACKEND_NAME` is exported for UI strings.
- On Linux, install output prints `loginctl enable-linger $USER` as the
  one-time setup if you want the daemon to run after logout (otherwise
  user units stop with the session, matching launchd's user-agent
  semantics).
- Linux logs go to `~/.watchmen/logs/{daemon,viewer}.{out,err}.log` (also
  available via `journalctl --user -u watchmen-daemon.service`).
- `--dry-run` for both backends now skips all preflight + filesystem
  side effects so you can audit the generated unit/plist on any platform.

### Added — pytest migration + adapter / agent coverage
- Test suite migrated from a hand-rolled `def check()` driver in
  `tests/smoke.py` to standard pytest discovery. `pytest tests/` is now
  the canonical entry point; existing `def test_*` functions kept their
  bodies — only the driver and module-level boilerplate moved out.
- `tests/conftest.py` owns the `src/` `sys.path` nudge + exports
  `ROOT` / `SRC` constants so individual tests can read package files.
- 30 new tests added across two previously zero-coverage modules:
  - `tests/test_adapter_claude_code.py` (11 tests) — exercises the
    Claude Code transcript parser with synthetic JSONL: user-prompt
    extraction, tool_use accounting, tool_error counting, cache-bucket
    handling, malformed-line tolerance, subagent threading.
  - `tests/test_agent.py` (19 tests) — fully mocked OpenRouter loop.
    Covers `load_api_key` (env → file → raise), `_backoff_seconds`
    (exponential + Retry-After floor), `call_openrouter` retry policy
    (429 / 500 retry, 400 fail-fast, RequestError exhaustion), Agent
    attribution headers, terminal-tool dispatch, non-terminal handler
    dispatch, `max_cost_usd` budget ceiling, max_iter exhaustion.
- CI workflow renamed `smoke` → `tests`; runs `pytest tests/ -q` across
  ubuntu × macos × py3.11/3.12 with `WATCHMEN_HOME` redirected to a temp
  dir and `OPENROUTER_API_KEY` explicitly unset so the api-key-resolution
  tests exercise their raise branch on a CI secret-free environment.
- `pytest>=8.0` + `pytest-cov>=5.0` added as `[project.optional-dependencies].dev`.
  Install once via `uv sync --extra dev`.

### Added — release plumbing
- `.github/workflows/release.yml` fires on `v*.*.*` tag pushes. Builds
  wheel + sdist, smoke-tests the wheel in a fresh venv, attaches both to
  a GitHub Release with auto-generated notes, and (when the
  `PUBLISH_TO_PYPI` repo variable is `true`) publishes to PyPI via OIDC
  trusted publishing — no API tokens stored anywhere.
- Added "Releasing" section to CONTRIBUTING.md documenting the tag flow.

### Changed
- README + CONTRIBUTING updated with the new `pytest tests/` workflow,
  pytest layout, `--cov` invocation, and cross-platform daemon notes.

## [0.4.0] — 2026-05-13

### Added — HTML insights viewer page (`/insights`)
- New page at `http://127.0.0.1:8979/insights` mirroring `watchmen
  insights` with the richer stats + charts that don't fit a terminal:
  - 4 headline tiles (sessions / tool errors / frustration markers /
    cost, each with a 30-day sparkline).
  - Adapter mix + skill-bundle coverage at a glance.
  - Per-repo table with skills, pending bundles, 30-day activity
    sparkline, adapter pills, tool-error count, frustration count,
    unanalyzed prompts.
  - Two horizontal bar charts: tool errors by repo + frustration
    markers by repo (server-rendered SVG, same aesthetic as the
    existing metrics page).
  - Cross-repo patterns table (slug × repos with ✓ curated / · candidate
    badges) and untapped corpora list.
  - Collapsible frustration samples — actual quoted prompts per repo,
    pulled straight from corpus.db.
  - Hour-of-day × day-of-week heatmap from `metrics.activity_by_hour_dow_all`.
  - The latest cached deep digest from `~/.watchmen/insights/` rendered
    as markdown (same content as `watchmen insights --view`).
- New nav link "Insights" in the viewer header, visible from every page.
- `metrics.hbar_chart_svg(rows, …)` reusable horizontal-bar SVG helper
  (XML-escaped labels, empty-input safe).

### Fixed
- Removed stale hardcoded `127.0.0.1:8888` from `base.html` footer —
  shows a generic "local viewer" label now (the actual port is whatever
  `watchmen settings port` is set to).

## [0.3.0] — 2026-05-13

### Added — harness-aware curator
- The candidate-finder now reads the user's installed Claude Code skills
  from `~/.claude/skills/*/SKILL.md` and is instructed to (a) prefer
  proposing an **enhancement** of an existing skill over a brand-new
  bundle when the trigger overlaps, and (b) compose existing skills
  rather than reinventing them. Each candidate may carry an optional
  `enhancement_of: <slug>` field that flows to Stage 2.
- New `harness.py` module: `installed_skills()`, `overlaps_existing()`,
  `format_for_prompt()`. Tiny surface area; tolerant of missing or
  malformed `SKILL.md` files.

### Added — conservative candidate prompt
- The finder prompt now requires a **recurring** pattern (within a
  single session OR across multiple sessions) before promoting any
  candidate to a skill. No numeric thresholds — the LLM stays in
  judgment mode, but with a clear "be ruthless, don't ship marginal
  bundles" instruction.

### Added — approval queue (`approval_required` setting)
- New per-project setting `approval_required: 0/1` (default 0 = autonomy
  preserved). When 1, **new** skill bundles route to
  `kai_claude/<project>/_pending/<slug>/` instead of
  `kai_claude/<project>/skills/<slug>/`. Already-approved skills in
  `skills/` keep updating in place — only first-time additions are gated.
- `watchmen review` now walks the pending queue first with
  `(a)pprove / (d)rop / (s)kip / (v)iew / (q)uit` semantics before
  continuing to the existing approved-skills walk. Approving moves
  `_pending/<slug>/` → `skills/<slug>/` (existing approved bundles are
  backed up to `<slug>.superseded/` for manual undo).
- New per-project setting `skip_overlapping_skills: 0/1` (default 0).
  When 1, candidates that overlap with installed harness skills are
  dropped entirely (no enhancement proposal).

### Added — CLI surface
- `watchmen curate <p> --skip-overlap` overrides the per-project setting.
- `watchmen curate <p> --approval-required` overrides the per-project setting.
- `watchmen settings set <p> approval_required true|false`
- `watchmen settings set <p> skip_overlapping_skills true|false`
- `watchmen settings show <p>` displays both new settings.

### Fixed
- `state.init_db()` migrates legacy `projects` rows to include the two
  new boolean columns (`approval_required`, `skip_overlapping_skills`).
  Pull + rerun applies the migration automatically; no manual ALTER
  TABLE needed.

## [0.2.0] — 2026-05-13

### Added
- **Release notes on first run after a version bump.** The CLI prints a
  compact "what's new in vX.Y.Z" block on the first invocation after a
  pull; the web viewer dashboard shows the same banner with a dismiss
  button. Last-seen version is tracked at `~/.watchmen/last_seen_version`
  (CLI) and `localStorage["watchmen.lastSeenVersion"]` (viewer).
- New `watchmen changelog` command renders the full CHANGELOG.md anytime.

### Removed
- **macOS notification briefs.** Deleted `brief.py` and
  `hooks/watchmen_brief.sh` along with the SessionStart hook entry that
  fired the osascript popup on every Claude Code session start. The
  surfaces that remain are intentional and non-intrusive:
  - `💡 watchmen` statusLine indicator (passive, when curator has news)
  - `/watchmen:brief` plugin skill (user-invoked, never auto)
- `watchmen hooks install` and `watchmen hooks uninstall` both scrub
  stale `watchmen_brief.sh` references from existing `settings.json`
  files automatically — no manual JSON editing needed.

## [0.1.1] — 2026-05-13

### Fixed
- **Auto-migrate `agent` column on every CLI startup.** Users with
  `corpus.db` files predating multi-adapter support (Codex + pi.dev) no
  longer hit `OperationalError: no such column: agent` on
  `watchmen insights`. Pull + rerun now picks up schema changes
  automatically; no `watchmen ingest --full` required.

## [0.1.0] — 2026-05-12

### Added
- `watchmen insights` — cross-repo deep digest. Static aggregation (repo
  table with sparklines, cross-repo candidate-slug overlaps, untapped
  corpora, friction signals from corpus.db) plus a 2-stage LLM pipeline
  (parallel per-repo synthesis → cross-repo digest) with view/regenerate
  caching at `~/.watchmen/insights/`.
- `README` section comparing watchmen to Claude Code's native `/insights`
  command — positions watchmen as the multi-adapter, persistent,
  per-repo layer.
- Friction signal helpers: tool-error counts and
  frustration-marker prompt regex (`no wait / bruh / :( / wtf / nope / …`)
  surface as a static row and feed the LLM pipeline.
- Watchmen aesthetic for `insights`: `◷` banner, `◆` section glyphs,
  `◉` deep-digest header, closing tagline rotating from a Manhattan /
  Rorschach / Veidt canon pool.
