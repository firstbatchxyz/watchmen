import { ReactNode } from 'react';
import { theme, fonts, sizes } from '../lib/theme';

/**
 * Mock Claude Code TUI chrome.  Top bar carries the agent label +
 * project name, body is the transcript area, bottom is reserved for
 * the statusLine (rendered as a sibling, not a child, so it can be
 * animated independently of the terminal contents).
 *
 * Sized to fill its parent — wrap in <AbsoluteFill> or a div with
 * explicit width/height.
 */
export const Terminal: React.FC<{
  projectName: string;
  agentLabel?: string;
  children: ReactNode;
}> = ({ projectName, agentLabel = 'claude code', children }) => {
  return (
    <div
      style={{
        width: '100%',
        height: '100%',
        background: theme.termBg,
        borderRadius: sizes.termRadius,
        overflow: 'hidden',
        display: 'flex',
        flexDirection: 'column',
        boxShadow: '0 24px 64px rgba(0,0,0,0.45)',
        fontFamily: fonts.mono,
      }}
    >
      {/* Top bar — agent + project, traffic-light dots on the left. */}
      <div
        style={{
          height: 56,
          background: theme.termBgRaised,
          display: 'flex',
          alignItems: 'center',
          padding: '0 24px',
          gap: 16,
          borderBottom: `1px solid rgba(255,255,255,0.06)`,
        }}
      >
        <div style={{ display: 'flex', gap: 8 }}>
          <Dot color="#FF5F57" />
          <Dot color="#FEBC2E" />
          <Dot color="#28C840" />
        </div>
        <div
          style={{
            color: theme.termFgSoft,
            fontSize: 20,
            letterSpacing: '0.04em',
            marginLeft: 12,
          }}
        >
          <span style={{ color: theme.termAccent, fontWeight: 600 }}>
            {agentLabel}
          </span>
          <span style={{ color: theme.termFgDim, margin: '0 10px' }}>·</span>
          <span style={{ color: theme.termFg }}>{projectName}</span>
        </div>
      </div>

      {/* Transcript body */}
      <div
        style={{
          flex: 1,
          padding: sizes.pad,
          color: theme.termFg,
          fontSize: sizes.termFontSize,
          lineHeight: sizes.termLineHeight,
          overflow: 'hidden',
        }}
      >
        {children}
      </div>
    </div>
  );
};

const Dot: React.FC<{ color: string }> = ({ color }) => (
  <div
    style={{
      width: 14,
      height: 14,
      borderRadius: '50%',
      background: color,
    }}
  />
);
