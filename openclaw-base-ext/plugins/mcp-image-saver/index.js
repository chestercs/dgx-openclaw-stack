// mcp-image-saver — makes images returned by MCP tools attachable in chat.
//
// An MCP tool can answer with an image block (a game screenshot, a chart).
// The model sees it, but a channel like Discord only attaches files: a reply
// line `MEDIA:<path>` with a path under an allowed media root. This tool
// result middleware writes every image of the configured tools into the
// canvas dir and appends a text line with its path, the same contract as the
// base-ext browser screenshot patch (`[saved: <path>]`). The image block stays
// in the result, so the model still looks at it.
//
// Config (plugins.entries["mcp-image-saver"].config, written by the patcher):
//   prefixes: tool-name prefixes to handle, e.g. ["valheim__"]
//   dir:      where to write (default the canvas, a media root for every agent)
//   keep:     newest files to keep per prefix (default 200; older ones go)
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { definePluginEntry } from 'openclaw/plugin-sdk/plugin-entry';

const DEFAULT_DIR = '/home/node/.openclaw/canvas';
const EXT = { 'image/png': 'png', 'image/jpeg': 'jpg', 'image/webp': 'webp', 'image/gif': 'gif' };

function prune(dir, stem, keep) {
  try {
    const files = fs.readdirSync(dir)
      .filter((f) => f.startsWith(`${stem}-`))
      .map((f) => ({ f, t: fs.statSync(path.join(dir, f)).mtimeMs }))
      .sort((a, b) => b.t - a.t);
    for (const { f } of files.slice(keep)) fs.rmSync(path.join(dir, f), { force: true });
  } catch { /* best effort */ }
}

export default definePluginEntry({
  id: 'mcp-image-saver',
  name: 'MCP Image Saver',
  description: 'Saves images returned by MCP tools to the canvas so replies can attach them.',
  register(api) {
    const cfg = api.pluginConfig ?? {};
    const prefixes = (Array.isArray(cfg.prefixes) ? cfg.prefixes : []).filter((p) => typeof p === 'string' && p);
    if (prefixes.length === 0) return;
    const dir = typeof cfg.dir === 'string' && cfg.dir ? cfg.dir : DEFAULT_DIR;
    const keep = Number.isInteger(cfg.keep) && cfg.keep > 0 ? cfg.keep : 200;

    api.registerAgentToolResultMiddleware(async (event) => {
      const prefix = prefixes.find((p) => String(event?.toolName ?? '').startsWith(p));
      const result = event?.result;
      if (!prefix || !Array.isArray(result?.content)) return undefined;
      const images = result.content.filter((b) => b?.type === 'image' && typeof b.data === 'string' && b.data);
      if (images.length === 0) return undefined;

      const stem = prefix.replace(/_+$/, '').replace(/[^a-z0-9-]/gi, '-');
      const stamp = new Date().toISOString().replace(/[-:]/g, '').replace(/\..*$/, '');
      const saved = [];
      fs.mkdirSync(dir, { recursive: true });
      for (const img of images) {
        const ext = EXT[img.mimeType] ?? 'png';
        const file = path.join(dir, `${stem}-${stamp}-${crypto.randomBytes(3).toString('hex')}.${ext}`);
        fs.writeFileSync(file, Buffer.from(img.data, 'base64'));
        saved.push(file);
      }
      prune(dir, stem, keep);
      const note = saved.map((f) => `[saved: ${f}]`).join('\n') +
        '\nTo show it in the chat, put this line on its own line in your reply: ' +
        saved.map((f) => `MEDIA:${f}`).join(' ');
      return { result: { ...result, content: [...result.content, { type: 'text', text: note }] } };
    }, { runtimes: ['openclaw'] });
  },
});
