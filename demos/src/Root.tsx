import { Composition } from 'remotion';
import { DemoD, DEMO_D_DURATION_FRAMES, DEMO_D_FPS } from './demos/DemoD';

// Every demo registers as a top-level Composition here.  Remotion Studio
// uses the `id` to route URLs; the render CLI uses it as the composition
// argument (`pnpm render:demo-d` → DemoD).
export const Root: React.FC = () => (
  <>
    <Composition
      id="DemoD"
      component={DemoD}
      durationInFrames={DEMO_D_DURATION_FRAMES}
      fps={DEMO_D_FPS}
      width={1920}
      height={1080}
    />
  </>
);
