/**
 * SINGLE SOURCE OF TRUTH for every byte of demo content.
 *
 * Hard rules:
 *   1. NEVER import from a real user path (`~/.watchmen/`, `~/.claude/`,
 *      `~/.codex/`, the production corpus.db).
 *   2. NEVER copy/paste real session transcripts, real prompts, real
 *      skill content from production projects.
 *   3. Project names + skill slugs must be obviously fake — no chance
 *      of being confused with a real customer or team's repo.
 *   4. Skill procedures must be generic (DB migrations, deploy ritual,
 *      API versioning).  Anything domain-specific = invent it.
 *
 * If you ever think "I'll just paste the real one for verisimilitude" —
 * stop and invent it.
 */

// ─── Fictional project shown in the demos ────────────────────────────
export const demoProject = {
  key: 'petstore-api',
  description: 'A pet store REST API in Node.js + Postgres',
  path: '~/code/petstore-api',
} as const;

// ─── Demo D: Retrospective coach (15s) ───────────────────────────────
// Scene-by-scene assets.  Lengths matter — typing animation timing is
// derived from the character count, so changing the prompt also
// shifts the downstream beats.

export const demoD = {
  // Long, fumbling, realistic user prompt.  The "ums" and self-corrections
  // are the point: they sell the "user wasn't sure what they were asking".
  prompt:
    'ok i need to add a column to the orders table but last time i did this in prod it locked everything for like 4 minutes can you walk me through... i think we use postgres 15... actually wait should i use add column with default or backfill separately, idk what\'s safer here, just help me figure this out',

  // Agent's response — short, helpful, correct.  Important narrative
  // beat: the agent IS helpful.  watchmen doesn't fix a broken agent;
  // it surfaces that a better path existed.
  agentResponse:
    'For Postgres 11+, `ALTER TABLE ... ADD COLUMN ... DEFAULT ...` doesn\'t rewrite the table — safe on big tables.\nFor large backfills, batch with `LIMIT 10000` outside a transaction.\nIndex on the new column with `CREATE INDEX CONCURRENTLY`.',

  // The skill watchmen suggests retrospectively.
  skill: {
    slug: 'db-migration-safe',
    title: 'Safe database migrations',
    description:
      'Add/alter columns in Postgres without locking the table. Covers ADD COLUMN, BACKFILL, INDEX CONCURRENTLY, and the four-step deploy ritual.',
    procedure: [
      'Pick the right ALTER variant — DEFAULT no longer rewrites the table on PG 11+',
      'Backfill in batches (LIMIT 10000, outside a transaction)',
      'CREATE INDEX CONCURRENTLY — never inline INDEX inside a migration',
      'Two PRs: schema first, application code second',
    ],
    sourceSessions: 4,
  },

  // StatusLine messages, in display order.
  statusLine: {
    idle: 'watchmen · ready',
    hint: '💡 you could have used /db-migration-safe to save time & tokens',
  },
} as const;
