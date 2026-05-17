import { useCurrentFrame, interpolate, spring, useVideoConfig } from 'remotion';
import { theme, fonts, sizes } from '../lib/theme';

/**
 * Bottom-right statusLine indicator, mimicking the watchmen plugin's
 * Claude Code statusLine slot.
 *
 * Two modes:
 *   - idle (e.g. "watchmen · ready"), muted color
 *   - hint (e.g. "💡 you could have used /<skill>..."), yellow accent
 *     with a subtle scale-up entrance
 *
 * Switches mode at `hintAtFrame`.  Before that frame, renders idle.
 * From `hintAtFrame` onward, animates the hint in via spring.
 */
export const StatusLine: React.FC<{
  idleText: string;
  hintText: string;
  hintAtFrame: number;
}> = ({ idleText, hintText, hintAtFrame }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const isHint = frame >= hintAtFrame;

  // Spring entrance for the hint — short, snappy.
  const enter = spring({
    frame: Math.max(0, frame - hintAtFrame),
    fps,
    config: { damping: 14, stiffness: 180, mass: 0.9 },
  });
  const hintScale = isHint ? interpolate(enter, [0, 1], [0.92, 1]) : 1;
  const hintOpacity = isHint ? interpolate(enter, [0, 1], [0, 1]) : 0;
  const idleOpacity = isHint ? 1 - Math.min(1, enter * 1.4) : 1;

  return (
    <div
      style={{
        position: 'absolute',
        bottom: 36,
        right: 56,
        fontFamily: fonts.mono,
        fontSize: sizes.statusFontSize,
        display: 'flex',
        alignItems: 'center',
        gap: 12,
        padding: '10px 18px',
        background: isHint ? 'rgba(245, 197, 24, 0.10)' : 'rgba(255,255,255,0.04)',
        border: `1px solid ${isHint ? theme.termAccent : 'rgba(255,255,255,0.08)'}`,
        borderRadius: 999,
        minWidth: 260,
        justifyContent: 'flex-end',
        transition: 'none',
      }}
    >
      {/* idle text (faded out by transition frame) */}
      {!isHint && (
        <span
          style={{
            color: theme.termFgDim,
            opacity: idleOpacity,
          }}
        >
          {idleText}
        </span>
      )}
      {/* hint text (spring-in) */}
      {isHint && (
        <span
          style={{
            color: theme.termAccent,
            opacity: hintOpacity,
            transform: `scale(${hintScale})`,
            transformOrigin: 'right center',
            fontWeight: 500,
          }}
        >
          {hintText}
        </span>
      )}
    </div>
  );
};
