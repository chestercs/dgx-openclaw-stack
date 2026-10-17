// people-context — puts the sender's own people/MEMORY.md section in front of
// every Discord turn of the discord-friend agent.
//
// people/MEMORY.md is already injected into the system prompt (patcher step
// 44), but Gemma 4 26B-A4B rarely connects "mit csinálok most?" with the
// sender's section there: given a channel history full of a recent task, it
// answered about the task instead. A before_prompt_build hook knows the
// sender ID (ctx.senderId) and can hand the model exactly the lines that
// matter, right next to the question. It also adds the sections of people
// tagged in the current message. A before_tool_call hook refuses direct
// edits of the file (see below). No reply hooks: `reply_payload_sending` /
// `message_sending` would turn off Discord preview streaming for every turn.
import fs from 'node:fs';
import path from 'node:path';
import { definePluginEntry } from 'openclaw/plugin-sdk/plugin-entry';

const AGENT_ID = process.env.PEOPLE_CONTEXT_AGENT || 'discord-friend';
const PEOPLE_FILE = 'people/MEMORY.md';
const MAX_TAGGED = 4;

const discordId = (v) => String(v ?? '').match(/\d{15,22}/)?.[0] ?? '';

// Map of Discord ID → { head, lines } for every `## Name (<@ID>)` section.
function readSections(file) {
  let text;
  try { text = fs.readFileSync(file, 'utf8'); } catch { return null; }
  const out = new Map();
  let cur = null;
  for (const line of text.split('\n')) {
    if (line.startsWith('## ')) {
      const id = line.match(/<@!?(\d+)>/)?.[1];
      cur = id ? { head: line.replace(/^##\s+/, '').trim(), lines: [] } : null;
      if (id) out.set(id, cur);
    } else if (cur && line.trim().startsWith('- ')) {
      cur.lines.push(line.trim());
    }
  }
  return out;
}

export default definePluginEntry({
  id: 'people-context',
  name: 'People Context',
  description: 'Prepends the Discord sender\'s people/MEMORY.md section to each turn.',
  register(api) {
    api.on('before_prompt_build', (event, ctx) => {
      if (ctx?.agentId !== AGENT_ID) return;
      const sender = discordId(ctx.senderId);
      if (!sender || !ctx.workspaceDir) return;
      const sections = readSections(path.join(ctx.workspaceDir, PEOPLE_FILE));
      if (!sections) return;

      const own = sections.get(sender);
      const parts = ['[Emberek-memória — a people/MEMORY.md idevágó sorai]'];
      if (own && own.lines.length) {
        parts.push(`A feladó: ${own.head}`, ...own.lines);
        parts.push('Ha a feladó MAGÁRÓL kérdez (mit csinál, hol van, mit tudsz róla), ezekből a sorokból válaszolj; ' +
          'az „ideiglenes” sor a jelenlegi állapota. Ez róla szól, nem a csatorna korábbi feladatairól.');
      } else {
        parts.push(`A feladóról (<@${sender}>) még nincs elmentett tény — ha magáról kérdez, mondd meg, hogy nem tudsz róla semmit.`);
      }
      // Only the admitted request: `prompt` may contain reconstructed history.
      const request = typeof event?.currentUserMessage === 'string' ? event.currentUserMessage : '';
      const tagged = [...new Set([...request.matchAll(/<@!?(\d{15,22})>/g)].map((m) => m[1]))]
        .filter((id) => id !== sender && sections.get(id)?.lines.length)
        .slice(0, MAX_TAGGED);
      for (const id of tagged) parts.push(`Megemlítve: ${sections.get(id).head}`, ...sections.get(id).lines);
      return { prependContext: parts.join('\n') };
    });

    // Gemma's own edit/write on the shared file once replaced every section,
    // and it still reached for `edit` with the router saying otherwise. The
    // guarded writer is /opt/people-digest.mjs note|forget (exec), so direct
    // file tools on people/MEMORY.md are refused with the right command.
    // The nightly job writes people/MEMORY.next.md, which stays allowed.
    api.on('before_tool_call', (event, ctx) => {
      if (ctx?.agentId !== AGENT_ID) return;
      const p = event?.params ?? {};
      const targets = [p.path, p.file_path, p.filePath, ...(Array.isArray(event?.derivedPaths) ? event.derivedPaths : [])];
      if (event?.toolName === 'apply_patch' && typeof p.input === 'string' && p.input.includes(PEOPLE_FILE)) targets.push(PEOPLE_FILE);
      if (!targets.some((t) => typeof t === 'string' && t.replace(/\\/g, '/').endsWith(PEOPLE_FILE))) return;
      return {
        block: true,
        blockReason: 'A people/MEMORY.md-t nem szerkesztheted közvetlenül. Mentés: exec `node /opt/people-digest.mjs note --id <ID> --name "<név>" --text "<tény>"`; ' +
          'javítás: ugyanez `--replace "<régi részlet>"`-tel; törlés: `node /opt/people-digest.mjs forget --id <ID> --match "<részlet>"`.',
      };
    }, { matcher: ['edit', 'write', 'apply_patch'] });
  },
});
