import { Config } from '@remotion/cli/config';

// Default output settings for `pnpm render:*` scripts.  Quality knobs live
// here so individual demo files don't have to repeat them.
Config.setVideoImageFormat('png');
Config.setOverwriteOutput(true);
Config.setPixelFormat('yuv420p');
Config.setCodec('h264');
