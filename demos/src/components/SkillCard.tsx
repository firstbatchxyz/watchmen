import { useCurrentFrame, spring, interpolate, useVideoConfig } from 'remotion';
import { theme, fonts, sizes } from '../lib/theme';

/**
 * SKILL.md preview card.  Slides up from the bottom with a spring,
 * styled as a comic-pulp cream panel with an ink border + offset
 * shadow (the same look the web viewer uses for `wm-card`).
 *
 * `startFrame` is when the slide-in begins.  `procedure` is a flat
 * list of strings; each renders as a numbered row.
 */
export const SkillCard: React.FC<{
  slug: string;
  title: string;
  description: string;
  procedure: readonly string[];
  sourceSessions: number;
  startFrame: number;
}> = ({ slug, title, description, procedure, sourceSessions, startFrame }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  const enter = spring({
    frame: Math.max(0, frame - startFrame),
    fps,
    config: { damping: 18, stiffness: 110, mass: 1.1 },
  });
  const translateY = interpolate(enter, [0, 1], [120, 0]);
  const opacity = interpolate(enter, [0, 0.4, 1], [0, 0.6, 1]);

  return (
    <div
      style={{
        position: 'absolute',
        left: '50%',
        bottom: 120,
        transform: `translateX(-50%) translateY(${translateY}px)`,
        opacity,
        width: 1180,
        background: theme.paper,
        color: theme.ink,
        border: `2px solid ${theme.ink}`,
        borderRadius: sizes.cardRadius,
        // Offset shadow — comic panel feel
        boxShadow: `8px 8px 0 0 ${theme.ink}`,
        fontFamily: fonts.body,
        padding: '32px 40px',
      }}
    >
      {/* Header row: slug pill + source count */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          marginBottom: 18,
        }}
      >
        <div
          style={{
            display: 'inline-flex',
            alignItems: 'center',
            gap: 12,
            background: theme.yellow,
            color: theme.ink,
            padding: '8px 18px',
            borderRadius: 8,
            fontFamily: fonts.mono,
            fontSize: 22,
            fontWeight: 700,
            border: `1.5px solid ${theme.ink}`,
            boxShadow: `3px 3px 0 0 ${theme.ink}`,
          }}
        >
          /{slug}
        </div>
        <div
          style={{
            fontFamily: fonts.mono,
            fontSize: 18,
            color: theme.inkSoft,
          }}
        >
          curated from {sourceSessions} sessions
        </div>
      </div>

      {/* Title */}
      <div
        style={{
          fontFamily: fonts.display,
          fontSize: sizes.cardTitle,
          fontWeight: 700,
          letterSpacing: '0.02em',
          textTransform: 'uppercase',
          color: theme.ink,
          marginBottom: 12,
        }}
      >
        {title}
      </div>

      {/* Description */}
      <div
        style={{
          fontSize: 22,
          color: theme.inkSoft,
          marginBottom: 24,
          lineHeight: 1.5,
        }}
      >
        {description}
      </div>

      {/* Procedure — numbered rows */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
        {procedure.map((step, i) => (
          <ProcedureRow key={i} idx={i + 1} text={step} />
        ))}
      </div>
    </div>
  );
};

const ProcedureRow: React.FC<{ idx: number; text: string }> = ({ idx, text }) => (
  <div
    style={{
      display: 'flex',
      alignItems: 'flex-start',
      gap: 16,
      fontSize: sizes.cardFontSize,
      color: theme.ink,
      lineHeight: 1.4,
    }}
  >
    <div
      style={{
        flexShrink: 0,
        width: 36,
        height: 36,
        borderRadius: '50%',
        background: theme.ink,
        color: theme.paper,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        fontFamily: fonts.mono,
        fontSize: 18,
        fontWeight: 700,
      }}
    >
      {idx}
    </div>
    <div style={{ paddingTop: 3 }}>{text}</div>
  </div>
);
