# watchmen — launch demos

Pure [Remotion](https://www.remotion.dev) compositions. **All data is mocked** — fake project keys, fake prompts, fake skills. Real session data never goes into a demo, ever. The single source of truth for mock content is `src/lib/mock-data.ts`.

## Why pure Remotion

Demos are short (15–90s), need to loop cleanly, and need cinematography (zoom, highlight, captioning). Real screen recordings are noisier and harder to iterate on. Pure Remotion compositions let us:

- Render the exact same scene at any resolution (1080p YouTube, 720p Twitter, vertical for Reels)
- Edit text/timing/skill-slug without re-recording
- Keep every demo deterministic — same input frame always produces the same output
- Treat the demos like code: PR review, version control, reproducible builds

## What's here

| Demo | Length | Status |
|---|---|---|
| **DemoD** — Retrospective coach | 15s loopable | ✅ scaffolded |
| DemoA — Agent disagreement | 90s | 🤔 queued |
| DemoB — CC → Codex switch | 60s | 🤔 queued |
| DemoC — Senior/junior handoff | 75s | 🤔 queued |
| DemoE — Day 1 vs Day 90 | 45s | 🤔 queued |

## Preview

```bash
cd demos
pnpm install         # also works with npm or bun
pnpm dev             # opens the Remotion Studio at http://localhost:3000
```

Studio lets you scrub the timeline, hot-reload edits, and export single frames or short ranges before committing to a full render.

## Render

```bash
pnpm render:demo-d        # mp4 → out/demo-d.mp4
pnpm render:demo-d-gif    # gif → out/demo-d.gif
```

`out/` is gitignored — renders are derived artifacts.

## Folder layout

```
demos/
  package.json
  tsconfig.json
  remotion.config.ts        # quality + codec defaults
  src/
    index.ts                # Remotion entry — registers Root
    Root.tsx                # registers every Composition
    lib/
      mock-data.ts          # SINGLE SOURCE for fake content
      theme.ts              # watchmen brand colors + sizes
    components/             # reusable scene pieces
      Terminal.tsx          # mock CC TUI chrome
      TypingText.tsx        # character-by-character typing animation
      StatusLine.tsx        # bottom-bar watchmen indicator
      SkillCard.tsx         # SKILL.md preview card
    demos/
      DemoD.tsx             # 15s retrospective-coach composition
```

## Data hygiene rules

Hard rules. Any PR that breaks these gets reverted:

1. **Never** import from `~/.watchmen/`, `~/.claude/`, `~/.codex/`, or any real path on disk.
2. **Never** copy/paste session transcripts, prompts, or skill content from real projects.
3. All mock content lives in `src/lib/mock-data.ts`. Demos import from there only.
4. Project names should be obviously fake — `petstore-api`, `blog-cms`, `wiki-search` — not anything that could be confused with a real customer or repo.
5. Skill slugs and procedures should be generic (DB migrations, API versioning, deploy ritual). No code that resembles a specific company's domain.

If you find yourself thinking "I could just paste the real one in for verisimilitude" — stop and invent it.

## Authoring conventions

- 30 fps, 1920×1080 by default. Vertical (1080×1920 for Shorts/Reels) is a follow-up.
- Use Remotion's `spring()` for natural motion, `interpolate()` for linear maps.
- Use `<Sequence>` for time-sliced scenes, `<Series>` for sequential.
- Match the watchmen comic-pulp brand for any in-frame UI (cream paper, Watchmen yellow, ink black, blood crimson). Tokens live in `lib/theme.ts`.
- A loopable demo should end on the same frame it starts — so autoplay platforms (Twitter, LinkedIn) loop seamlessly.
