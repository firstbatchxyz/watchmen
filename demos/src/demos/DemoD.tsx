import { AbsoluteFill, interpolate, Sequence, useCurrentFrame } from 'remotion';
import { theme, fonts } from '../lib/theme';
import { demoProject, demoD } from '../lib/mock-data';
import { Terminal } from '../components/Terminal';
import { TypingText } from '../components/TypingText';
import { StatusLine } from '../components/StatusLine';
import { SkillCard } from '../components/SkillCard';

/**
 * Demo D — Retrospective coach (15s loopable).
 *
 * Narrative beats (30 fps):
 *   0:00–0:01  (frames   0–30) — establish: empty terminal, cursor blinks
 *   0:01–0:05  (frames  30–150) — user types the long fumbling prompt
 *   0:05–0:06  (frames 150–180) — agent thinking dots
 *   0:06–0:09  (frames 180–270) — agent response prints
 *   0:09–0:11  (frames 270–330) — beat; then statusLine refreshes to hint
 *   0:11–0:14  (frames 330–420) — SkillCard slides up from bottom
 *   0:14–0:15  (frames 420–450) — fade everything out; loops cleanly
 *
 * Loop seam: at frame 0 and frame 450 the canvas is the same paper-cream
 * background with nothing drawn.  Autoplay platforms loop seamlessly.
 */

export const DEMO_D_FPS = 30;
export const DEMO_D_DURATION_FRAMES = 450; // 15 seconds at 30 fps

// Scene boundaries — pulled out so the source reads top-to-bottom.
const F = {
  promptStart: 30,
  promptCps: 38,                        // chars/sec; tuned to fit 0:01–0:05
  promptEnd: 150,
  agentThinkStart: 152,
  agentResponseStart: 182,
  agentResponseCps: 42,
  statusHintAt: 280,
  skillCardAt: 330,
  fadeOutAt: 420,
} as const;

export const DemoD: React.FC = () => {
  const frame = useCurrentFrame();

  // Global fade — punched in for the last 1s so the loop has a clean
  // exit/entry on the same blank-paper frame.
  const fadeAlpha = interpolate(
    frame,
    [F.fadeOutAt, DEMO_D_DURATION_FRAMES - 1],
    [1, 0],
    { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' },
  );

  return (
    <AbsoluteFill style={{ background: theme.paper }}>
      {/* Subtle halftone backdrop — sells the comic-pulp brand. */}
      <Halftone />

      {/* Inset wrapper that the terminal + statusLine share, so the
          statusLine sits inside the terminal's bottom-right corner. */}
      <AbsoluteFill
        style={{
          padding: 80,
          opacity: fadeAlpha,
        }}
      >
        <div
          style={{
            position: 'relative',
            width: '100%',
            height: '100%',
          }}
        >
          <Terminal projectName={demoProject.key}>
            {/* User prompt — types out in scene 1 */}
            <div style={{ marginBottom: 24 }}>
              <TypingText
                text={demoD.prompt}
                startFrame={F.promptStart}
                cps={F.promptCps}
                prefix="> "
                color={theme.termFg}
                showCursor={frame < F.agentThinkStart}
              />
            </div>

            {/* Agent thinking indicator — short beat */}
            <Sequence from={F.agentThinkStart} durationInFrames={F.agentResponseStart - F.agentThinkStart}>
              <ThinkingDots />
            </Sequence>

            {/* Agent response — prints character-by-character */}
            <Sequence from={F.agentResponseStart}>
              <div style={{ color: theme.termFgSoft, marginBottom: 16 }}>
                <TypingText
                  text={demoD.agentResponse}
                  startFrame={F.agentResponseStart}
                  cps={F.agentResponseCps}
                  showCursor={false}
                  color={theme.termFgSoft}
                />
              </div>
            </Sequence>
          </Terminal>

          {/* StatusLine — idle → hint at F.statusHintAt */}
          <StatusLine
            idleText={demoD.statusLine.idle}
            hintText={demoD.statusLine.hint}
            hintAtFrame={F.statusHintAt}
          />
        </div>
      </AbsoluteFill>

      {/* SkillCard — slides up after the hint lands.  Sits above the
          terminal so the "user clicked the hint" beat reads. */}
      <Sequence from={F.skillCardAt}>
        <AbsoluteFill style={{ opacity: fadeAlpha }}>
          <SkillCard
            slug={demoD.skill.slug}
            title={demoD.skill.title}
            description={demoD.skill.description}
            procedure={demoD.skill.procedure}
            sourceSessions={demoD.skill.sourceSessions}
            startFrame={F.skillCardAt}
          />
        </AbsoluteFill>
      </Sequence>
    </AbsoluteFill>
  );
};

// ─── Helpers ────────────────────────────────────────────────────────

/**
 * Three-dot thinking indicator, animated via frame modulo to feel
 * alive without a real spring.  Renders inline in the terminal body.
 */
const ThinkingDots: React.FC = () => {
  const frame = useCurrentFrame();
  const phase = Math.floor(frame / 8) % 4;
  const dots = '.'.repeat(phase);
  return (
    <div style={{ color: theme.termFgDim, fontFamily: fonts.mono }}>
      {`thinking${dots}`}
    </div>
  );
};

/**
 * Halftone-dot backdrop.  Pure CSS radial-gradient grid — matches the
 * web viewer's `--bg-pattern` token.  Sits behind everything at 6%
 * alpha so it reads as texture, not pattern.
 */
const Halftone: React.FC = () => (
  <AbsoluteFill
    style={{
      backgroundImage:
        'radial-gradient(circle, rgba(19,19,22,0.06) 1.2px, transparent 1.6px)',
      backgroundSize: '14px 14px',
    }}
  />
);
