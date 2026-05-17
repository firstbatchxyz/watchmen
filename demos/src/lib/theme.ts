/**
 * Watchmen comic-pulp brand tokens, mirrored from the web viewer's
 * base.html `:root` HSL palette but baked here as hex values so each
 * Remotion render is self-contained.  When the viewer brand evolves,
 * resync these by hand — that's intentional; we want demos pinned to
 * the brand-as-of-release-time, not whatever's currently on main.
 */
export const theme = {
  // Paper / ink (comic-pulp light)
  paper: '#F1E8D2',
  paperShadow: '#E3D5B0',
  ink: '#131316',
  inkSoft: '#3D3E45',

  // Brand accents
  yellow: '#F5C518',       // Watchmen smiley yellow
  yellowDeep: '#C99B0F',
  blood: '#BC1D2E',        // smiley blood-streak / urgency
  bloodDeep: '#8A1521',

  // Muted / subtle
  muted: '#9B9690',
  mutedSoft: '#C8C3BC',

  // Terminal (dark) — Claude Code TUI mock
  termBg: '#0C0D11',
  termBgRaised: '#16171D',
  termFg: '#E8E9EE',
  termFgSoft: '#A9A9B0',
  termFgDim: '#6B6B70',
  termAccent: '#F5C518',
  termCursor: '#F5C518',
  termSuccess: '#8FCB6E',
  termBlood: '#DC2832',
} as const;

// Typography
export const fonts = {
  mono: '"JetBrains Mono", "Menlo", "Monaco", monospace',
  display: '"Oswald", "Inter", system-ui, sans-serif',
  body: '"Inter", system-ui, sans-serif',
} as const;

// Size scale — pixel-based; demos are 1920×1080 so we can be liberal.
export const sizes = {
  termFontSize: 28,
  termLineHeight: 1.5,
  statusFontSize: 22,
  cardFontSize: 26,
  cardTitle: 38,
  pad: 32,
  cardRadius: 12,
  termRadius: 16,
} as const;
