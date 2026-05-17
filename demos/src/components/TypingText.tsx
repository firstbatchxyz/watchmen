import { useCurrentFrame, useVideoConfig } from 'remotion';
import { theme } from '../lib/theme';

/**
 * Character-by-character typing animation driven by the current frame.
 * `cps` = characters per second.  The full string lands at
 * `Math.ceil(text.length / cps * fps)` frames after `startFrame`.
 *
 * After the text is fully typed, the cursor sticks around for
 * `cursorLingerFrames` so the next scene has a beat to react to.
 *
 * Set `showCursor={false}` for non-input lines (agent replies, etc.).
 */
export const TypingText: React.FC<{
  text: string;
  startFrame?: number;
  cps?: number;
  showCursor?: boolean;
  color?: string;
  prefix?: string;     // e.g. "> " for a prompt line
  prefixColor?: string;
}> = ({
  text,
  startFrame = 0,
  cps = 36,
  showCursor = true,
  color = theme.termFg,
  prefix,
  prefixColor = theme.termAccent,
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  const elapsed = Math.max(0, frame - startFrame);
  const charsToShow = Math.min(text.length, Math.floor((elapsed / fps) * cps));
  const shown = text.slice(0, charsToShow);
  const done = charsToShow >= text.length;

  // Blink the cursor at 2 Hz — feels alive without being distracting.
  const cursorVisible = Math.floor(frame / Math.max(1, Math.floor(fps / 4))) % 2 === 0;

  return (
    <div style={{ color, whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
      {prefix !== undefined && (
        <span style={{ color: prefixColor, fontWeight: 600 }}>{prefix}</span>
      )}
      {shown}
      {showCursor && (
        <span
          style={{
            color: theme.termCursor,
            opacity: !done || cursorVisible ? 1 : 0,
            marginLeft: 2,
          }}
        >
          ▌
        </span>
      )}
    </div>
  );
};
