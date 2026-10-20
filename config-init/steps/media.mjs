// Optional media surfaces: Fish TTS, the ComfyUI bridge (image / video /
// music), and the prompt text that teaches the agents to use them. Each one
// is off unless .env enables it.
//
// OPENCLAW_DISCORD_MEDIA_TOOLS_OFF (comma list of image, video, music) names
// the groups ComfyUI does not run right now: a large LLM backend leaves no
// room for renders, and the bot kept calling the dead tools (once with the
// wrong prompt). A group listed there gets no skill here and an "off" line in
// the Discord router (steps/workspaces.mjs); the tool deny lives with tools.

import fs from 'node:fs';
import path from 'node:path';
import * as env from '../lib/env.mjs';
import { secret } from '../lib/secrets.mjs';
import { template, upsertBlock, skill, removeSkill, removeFile } from '../lib/files.mjs';

const GROUPS = ['image', 'video', 'music'];
export const mediaOn = (group) => !env.mediaOff().includes(group);

const DESC = {
  'image-generation': 'Image generation with the comfyui_image__generate tool — workflow choice (SFW/adult), resolution recipes (2K/FullHD/portrait/square), the display_markdown delivery contract.',
  'image-to-image': 'Transforming/editing an attached image with the comfyui_image__generate_i2i tool ("alakítsd át", "csinálj belőle", "make it look like") — init_image_url path, denoise scale, adult workflow variant.',
  'video-generation': 'Video generation with the comfyui_image__generate_video tool (LTX-Video 2.3) — T2V vs I2V routing, the resolution arg, length/fps, mp4 delivery. The tool exists even before the operator has set up the workflows — say so on an error.',
  'music-generation': 'Music generation with the comfyui_image__generate_music tool (ACE-Step 1.5) — tags (required) + optional lyrics, instrumental or vocal; Discord delivery via upload-file from canvas_path (NOT a link, /view is auth-walled). Repaint: generate_music_repaint on attached audio.',
};

// Writes the skill when `on`, removes it otherwise (skill files are wholly
// patcher-owned, so a retracted feature leaves nothing behind).
function placeSkill(ws, name, on, body) {
  if (on) skill(ws, name, DESC[name], body());
  else removeSkill(ws, name);
}

// Fish Audio S2 Pro behind the openclaw-tts-fish shim (OpenAI Audio API
// shape). Written when OPENCLAW_TTS_FISH_API_KEY is set; OPENCLAW_TTS=off
// removes the whole top-level `tts` key; with neither, `tts` is left alone.
function tts(config) {
  const key = env.str('OPENCLAW_TTS_FISH_API_KEY');
  const on = env.bool('OPENCLAW_TTS', Boolean(key));
  if (!on) {
    if (env.raw('OPENCLAW_TTS') !== undefined) config.remove('tts', 'OPENCLAW_TTS=off');
    return;
  }
  if (!key) throw new Error('OPENCLAW_TTS=on needs OPENCLAW_TTS_FISH_API_KEY');
  config.merge('tts', {
    // On Discord text channels use `tagged`: with `always` every final reply
    // gets an audio attachment, and a failed transcode drops the text too.
    auto: env.oneOf('OPENCLAW_TTS_AUTO', ['off', 'always', 'inbound', 'tagged'], 'always'),
    mode: 'final',
    // The gateway's 30 s default times out on Fish's ~2000-char chunks
    // (60-110 s each on the GB10); the schema caps the field at 120000.
    timeoutMs: Math.min(env.int('OPENCLAW_TTS_TIMEOUT_MS', 120000), 120000),
  });
  config.merge('tts.providers.openai', {
    baseUrl: env.str('OPENCLAW_TTS_FISH_URL', 'http://openclaw-tts-fish:8080/v1'),
    apiKey: secret('OPENCLAW_TTS_FISH_API_KEY', key),
    model: 'fish-s2-pro', // the shim ignores it; logged for debugging
    speakerVoiceId: env.str('OPENCLAW_TTS_DEFAULT_VOICE', 'default_en'),
  });
  // Friendly names for the voices the image seeds (tts-fish-voices volume);
  // raw file basenames work too.
  config.merge('tts.providers.openai.voiceAliases', {
    english: 'default_en', narrator: 'default_en', magyar: 'default_hu', hungarian: 'default_hu',
    female: 'bella', male: 'michael', british: 'emma', deep: 'fenrir', soft: 'nicole',
  });
}

// The openclaw-image-comfyui bridge (its own compose file, joined to this
// stack's network). Wired when IMAGE_GEN_API_TOKEN is set, removed otherwise
// so the gateway does not dial a parked bridge.
function comfyuiMcp(config) {
  const token = env.str('IMAGE_GEN_API_TOKEN');
  const p = ['mcp', 'servers', 'comfyui_image'];
  if (!token) {
    config.remove(p, 'IMAGE_GEN_API_TOKEN unset');
    if (config.get('mcp.servers') && !Object.keys(config.get('mcp.servers')).length) config.remove('mcp.servers');
    if (config.get('mcp') && !Object.keys(config.get('mcp')).length) config.remove('mcp');
    return;
  }
  config.merge(p, {
    transport: 'streamable-http',
    url: env.str('IMAGE_GEN_URL', 'http://openclaw-image-comfyui:9095/mcp'),
    connectionTimeoutMs: 10000,
    // Must outlast the bridge's own 900 s render budget, so the bridge's
    // timeout + cancel answers first with a clean error instead of the
    // client abandoning a render the bot then promises.
    requestTimeoutMs: env.int('IMAGE_GEN_MCP_REQUEST_TIMEOUT_MS', 960000),
  });
  // Set by key, not as a headers object, so the log masks the token.
  config.set([...p, 'headers', 'Authorization'], `Bearer ${token}`);
}

export default function media({ config, state, ws }) {
  tts(config);
  comfyuiMcp(config);

  const on = Object.fromEntries(GROUPS.map((g) => [g, mediaOn(g)]));
  // Image recipes point at bundle workflows, so they wait for the operator to
  // name the default one; video waits for LTX_VIDEO_ENABLED (set once the
  // ~71 GB model download has finished).
  const workflow = env.str('IMAGE_GEN_DEFAULT_WORKFLOW');
  const ltx = env.bool('LTX_VIDEO_ENABLED', false) && on.video;
  const frames = env.str('LTX_VIDEO_DEFAULT_LENGTH_FRAMES', '145');
  const fps = env.str('LTX_VIDEO_DEFAULT_FPS', '24');
  const video = () => template('skills/video-generation.md', {
    LENGTH_FRAMES: frames, FPS: fps, SECONDS: (parseFloat(frames) / parseFloat(fps)).toFixed(1),
    MAX_DURATION_S: env.str('LTX_VIDEO_MAX_DURATION_S', '10'),
  });

  const discordMd = path.join(ws('discord'), 'AGENTS.md');
  if (fs.existsSync(discordMd)) {
    const d = ws('discord');
    placeSkill(d, 'image-generation', Boolean(workflow) && on.image,
      () => template('skills/image-generation.md', { DEFAULT_WORKFLOW: workflow }));
    placeSkill(d, 'image-to-image', on.image, () => template('skills/image-to-image.md'));
    placeSkill(d, 'video-generation', ltx, video);
    placeSkill(d, 'music-generation', on.music, () => template('skills/music-generation.md'));
    // About vision inputs, not generation, so it stays when image is off:
    // past LLM_LIMIT_MM_IMAGES per prompt vLLM answers 400 and the turn dies.
    upsertBlock(discordMd, 'discord-image-history-rule', template('discord/AGENTS.d/discord-image-history-rule.md'));
  }
  // The CLI-routed main agent gets the same video recipe (it teaches the
  // `resolution` arg the model otherwise drops when rewriting the prompt).
  if (fs.existsSync(ws())) placeSkill(ws(), 'video-generation', ltx, video);

  // The `!img` bash command is retired (the claw-img bot's slash commands
  // replace it); delete the script an older patcher left behind.
  removeFile(path.join(state, 'bin', 'img'));
}
