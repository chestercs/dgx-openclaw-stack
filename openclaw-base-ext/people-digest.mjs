#!/usr/bin/env node
// people-digest — feeds the nightly people-memory job of the Discord agent.
//
// The job (an `openclaw cron` agent turn, see the `people-memory` workspace
// skill written by patch-config.mjs step 44) keeps people/MEMORY.md up to
// date from what guild members say in ordinary channel traffic, not only
// from what they tell the bot directly. Two subcommands:
//
//   collect --guild <id>[,<id>…]|all [--nsfw] [--hours 24 | --from <date> [--to <date>] | --last-days N] [--exclude a,b] [--max-chars 60000]
//     Reads every text channel of the guild(s) since the last committed cursor
//     (first run: the last --hours) through `openclaw message read` and
//     prints a compact per-author digest. A 26B model calling `message read`
//     thirty times itself was slow and lost track; one pre-digested block is
//     cheaper and deterministic. Dropped: our own bot (feedback loop), NSFW
//     channels (the file reaches every channel; --nsfw or PEOPLE_DIGEST_NSFW=on
//     keeps them) and --exclude channels (names or IDs). Other bots stay, marked `(bot)`: the members
//     talk to them (a Markov chat bot), so their lines are context, not
//     facts. Voice-log lines ("<@id> joined voice channel", Dyno-style
//     embeds) are folded into one per-member activity summary instead of
//     hundreds of raw lines. PEOPLE_DIGEST_LOGS=off restores the old rule
//     that skipped every `*log` channel.
//
//   apply [--no-change]
//     Validates the model's proposal (people/MEMORY.next.md) against the
//     live file and swaps it in only when no `## person` section vanished,
//     every "végleges szabály" line survived and the file did not shrink
//     below 70 %. A model rewrite once dropped six people in one edit; this
//     gate makes that a rejected proposal instead of lost memory. The live
//     file is backed up first. Read cursors are committed here, so a run
//     that never reaches `apply` re-reads the same window next night.
//
//   history --channel <id> [--author <name|id>] [--grep <text>] [--limit 20] [--days N] [--before <msg id>]
//     The bot's channel-history search: pages back, keeps embed text, one
//     compact line per match, and states the scanned range (see history()).
//
//   note --id <discord id> --name <name> --text <fact> [--temp|--durable] [--replace <part>]
//   forget --id <discord id> (--match <part> | --all)
//     The bot's "remember this" path. Gemma editing the shared file itself
//     failed twice: an `edit` anchored on a section that did not exist yet,
//     then a fallback `write` that replaced the whole file. These commands
//     find or create the person's section and change one line. A temporary
//     state (--temp, or present-state wording, see TEMP_RE) becomes a dated
//     `(ideiglenes, YYYY-MM-DD)` line that replaces the person's older one.
//
// Every command first heals the live file against last-good.md, the copy
// saved after the last change made through this script. If a stray `write`
// dropped a section or a permanent rule, last-good comes back (the damaged
// file is kept in history/). If only the header comment is missing, it is
// put back.
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';

const HOME = '/home/node/.openclaw';
const WS = process.env.PEOPLE_DIGEST_WORKSPACE || path.join(HOME, 'workspace-discord');
const LIVE = path.join(WS, 'people', 'MEMORY.md');
const NEXT = path.join(WS, 'people', 'MEMORY.next.md');
const STATE_DIR = process.env.PEOPLE_DIGEST_STATE || path.join(HOME, 'people-digest');
const STATE = path.join(STATE_DIR, 'state.json');
const HISTORY = path.join(STATE_DIR, 'history');
const LAST_GOOD = path.join(STATE_DIR, 'last-good.md');
const DISCORD_EPOCH = 1420070400000n;
const MAX_LOOKBACK_HOURS = 72;

function args(argv) {
  const out = { _: [] };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a.startsWith('--')) {
      const next = argv[i + 1];
      if (next === undefined || next.startsWith('--')) out[a.slice(2)] = true;
      else { out[a.slice(2)] = next; i++; }
    } else out._.push(a);
  }
  return out;
}

function loadState() {
  try { return JSON.parse(fs.readFileSync(STATE, 'utf8')); } catch { return { cursors: {} }; }
}
function saveState(state) {
  fs.mkdirSync(STATE_DIR, { recursive: true });
  fs.writeFileSync(STATE + '.tmp', JSON.stringify(state, null, 2));
  fs.renameSync(STATE + '.tmp', STATE);
}

function openclaw(argv) {
  const raw = execFileSync('openclaw', [...argv, '--json'], {
    encoding: 'utf8', maxBuffer: 64 * 1024 * 1024, stdio: ['ignore', 'pipe', 'ignore'],
  });
  return JSON.parse(raw);
}

// The CLI wraps payloads differently per command; take the first array found.
function firstArray(o) {
  if (Array.isArray(o)) return o;
  if (o && typeof o === 'object') {
    for (const v of Object.values(o)) {
      const hit = firstArray(v);
      if (hit) return hit;
    }
  }
  return null;
}

// Our own bot's user id, so its replies never feed back into the file.
// `openclaw channels status` lists the logged-in account; the env override is
// for setups where that call is unavailable. Unknown → every bot is skipped.
function selfId() {
  if (process.env.PEOPLE_DIGEST_SELF_ID) return process.env.PEOPLE_DIGEST_SELF_ID;
  try {
    const seen = [];
    const walk = (o) => {
      if (!o || typeof o !== 'object') return;
      if (typeof o.id === 'string' && /^\d{15,}$/.test(o.id) && typeof o.username === 'string') seen.push(o.id);
      for (const v of Object.values(o)) walk(v);
    };
    walk(openclaw(['channels', 'status']));
    return seen[0] || null;
  } catch { return null; }
}

// Text of a message: its content, else its embeds (log bots post embeds).
// A slash-command reply names who ran the command only in its interaction
// metadata (a RuinBot win embed says "kapta …", the winner is the user who
// ran /ruin), so that is appended.
// Embed fields become "name: value | name: value": bots like RuinBot end
// field names with ":" and pad with invisible spacer fields (soft hyphen,
// LRM), which ran together as "szín:: Copper Rust ­: ­ ennyiből…" and the bot
// mixed up two adjacent win lines on 2026-10-20.
const INVISIBLE = /[­​-‏⁠﻿]/g;
const clean = (s) => String(s ?? '').replace(INVISIBLE, '').replace(/\s+/g, ' ').trim();
function embedText(e) {
  const fields = (e.fields || [])
    .map((f) => [clean(f.name).replace(/:+$/, ''), clean(f.value)])
    .filter(([n, v]) => n || v)
    .map(([n, v]) => (n && v ? `${n}: ${v}` : n || v));
  const head = [clean(e.title), clean(e.description)].filter(Boolean).join(' — ');
  return [head, fields.join(' | ')].filter(Boolean).join(' — ');
}
function messageText(m) {
  const own = String(m.content || '').trim();
  const text = own || (m.embeds || []).map(embedText).filter(Boolean).join(' || ').trim();
  const ia = m.interaction_metadata ?? m.interaction;
  const by = ia?.user?.global_name || ia?.user?.username;
  return by && text ? `${text} [/${ia.name || 'parancs'} — ${by}]` : text;
}

const VOICE_RE = /<@!?(\d+)>\*{0,2}\s+(joined|left|switched)\s+voice channel/i;
const hhmm = (iso) => new Date(iso).toLocaleTimeString('hu-HU', { timeZone: 'Europe/Budapest', hour: '2-digit', minute: '2-digit' });
const ymd = (iso) => new Date(iso).toLocaleDateString('sv-SE', { timeZone: 'Europe/Budapest' }); // YYYY-MM-DD, Budapest

const snowflakeAt = (ms) => ((BigInt(ms) - DISCORD_EPOCH) << 22n).toString();
const maxId = (a, b) => (BigInt(a) > BigInt(b) ? a : b);

// `--guild all`: every guild the bot is in. The OpenClaw CLI has no guild
// list, so ask Discord directly with the bot token from openclaw.json (this
// script runs as the gateway's user, which owns that file).
async function resolveGuilds(spec) {
  if (spec !== 'all') {
    const ids = spec.split(',').map((s) => s.trim()).filter(Boolean);
    if (!ids.length || !ids.every((id) => /^\d{5,}$/.test(id))) {
      throw new Error('collect needs --guild <id>[,<id>…] | all (or PEOPLE_DIGEST_GUILD)');
    }
    return ids.map((id) => ({ id, name: null }));
  }
  const token = discordToken();
  if (!token) throw new Error('--guild all needs a literal channels.discord.token in openclaw.json; pass guild ids instead');
  const res = await fetch('https://discord.com/api/v10/users/@me/guilds?limit=200', { headers: { Authorization: `Bot ${token}` } });
  if (!res.ok) throw new Error(`Discord guild list: HTTP ${res.status}`);
  return (await res.json()).map((g) => ({ id: g.id, name: g.name }));
}

// The bot token from openclaw.json, or null when it is a ${VAR} reference.
function discordToken() {
  try {
    const token = JSON.parse(fs.readFileSync(path.join(HOME, 'openclaw.json'), 'utf8')).channels?.discord?.token;
    return typeof token === 'string' && token && !token.startsWith('${') ? token : null;
  } catch { return null; }
}

// One page of a channel, newest first. Discord REST takes well under a second;
// each `openclaw message read` spawns a CLI (~15 s), the fallback when the
// token is not in the file.
async function readPage(channel, before) {
  const token = discordToken();
  if (token) {
    const q = new URLSearchParams({ limit: '100', ...(before ? { before } : {}) });
    const res = await fetch(`https://discord.com/api/v10/channels/${channel}/messages?${q}`, { headers: { Authorization: `Bot ${token}` } });
    if (!res.ok) throw new Error(`Discord channel read: HTTP ${res.status}`);
    return res.json();
  }
  const argv = ['message', 'read', '--channel', 'discord', '--target', `channel:${channel}`, '--limit', '100'];
  if (before) argv.push('--before', before);
  return firstArray(openclaw(argv)) || [];
}

async function collect(opt) {
  heal();
  const guilds = await resolveGuilds(String(opt.guild || process.env.PEOPLE_DIGEST_GUILD || ''));
  const hours = Math.min(Number(opt.hours) || 24, MAX_LOOKBACK_HOURS);
  const maxChars = Number(opt['max-chars']) || 60000;
  const exclude = new Set(String(opt.exclude || '').split(',').map((s) => s.trim()).filter(Boolean));
  const state = loadState();
  const floor = snowflakeAt(Date.now() - MAX_LOOKBACK_HOURS * 3600e3);
  const start = snowflakeAt(Date.now() - hours * 3600e3);
  // Backfill: --from/--to (dates) read a fixed past window, past the 72 h cap,
  // and leave the nightly cursors untouched (no pending write, so `apply`
  // commits nothing). Used once to learn from older history in weekly slices.
  // --last-days N is the same window ending now (the community-summary job).
  if (opt['last-days'] !== undefined && opt.from === undefined) {
    opt.from = new Date(Date.now() - Number(opt['last-days']) * 86400e3).toISOString();
  }
  const windowed = opt.from !== undefined;
  const fromMs = windowed ? Date.parse(opt.from) : NaN;
  const toMs = windowed && opt.to !== undefined ? Date.parse(opt.to) : Date.now();
  if (windowed && (Number.isNaN(fromMs) || Number.isNaN(toMs) || fromMs >= toMs)) throw new Error('--from/--to need valid dates, from < to');
  const winEnd = windowed ? BigInt(snowflakeAt(toMs)) : null;
  const maxPages = Number(opt.pages) || (windowed ? 30 : 5);

  const keepNsfw = opt.nsfw === true || /^(on|1|true|yes)$/i.test(process.env.PEOPLE_DIGEST_NSFW || '');
  const skipLogs = opt['skip-logs'] === true || /^(off|0|false|no)$/i.test(process.env.PEOPLE_DIGEST_LOGS || '');
  const channels = [];
  for (const g of guilds) {
    let list;
    try {
      list = firstArray(openclaw(['message', 'channel', 'list', '--channel', 'discord', '--guild-id', g.id])) || [];
    } catch {
      continue; // left the guild / no permission: skip it
    }
    for (const c of list) {
      if ((c.type === 0 || c.type === 5) && (keepNsfw || !c.nsfw) && !exclude.has(c.name) && !exclude.has(c.id)
        && !(skipLogs && /(^|-)log$/.test(c.name))) {
        // With several guilds, tag lines with the guild so the model can tell
        // which community a fact came from.
        channels.push({ ...c, label: guilds.length > 1 ? `${g.name || g.id}/${c.name}` : c.name });
      }
    }
  }
  const self = selfId();

  const authors = new Map();
  const voice = new Map(); // member id -> { joined, left, switched, times: [] }
  const pending = {};
  let total = 0;
  for (const ch of channels) {
    const cursor = state.cursors[ch.id];
    let after = windowed ? snowflakeAt(fromMs) : (cursor ? maxId(cursor, floor) : start);
    let last = after;
    let pastEnd = false;
    for (let page = 0; page < maxPages && !pastEnd; page++) {
      let msgs;
      try {
        msgs = firstArray(openclaw(['message', 'read', '--channel', 'discord', '--target', `channel:${ch.id}`, '--after', after, '--limit', '100'])) || [];
      } catch {
        break; // missing permission / deleted channel: skip, keep old cursor
      }
      if (msgs.length === 0) break;
      msgs.sort((a, b) => (BigInt(a.id) < BigInt(b.id) ? -1 : 1));
      for (const m of msgs) {
        if (winEnd !== null && BigInt(m.id) >= winEnd) { pastEnd = true; break; }
        last = maxId(last, m.id);
        if (!m.author || (m.author.bot && (!self || m.author.id === self))) continue;
        const text = messageText(m).replace(/\s+/g, ' ').trim();
        if (!text || /^(https?:\/\/\S+\s*)+$/.test(text)) continue;
        const v = m.author.bot && text.match(VOICE_RE);
        if (v) {
          const s = voice.get(v[1]) ?? { joined: 0, left: 0, switched: 0, times: [] };
          s[v[2].toLowerCase()]++;
          s.times.push(m.timestamp);
          voice.set(v[1], s);
          total++;
          continue;
        }
        const id = m.author.id;
        const a = authors.get(id) ?? { id, bot: !!m.author.bot, name: m.author.global_name || m.author.username, user: m.author.username, msgs: [] };
        a.msgs.push({ ch: ch.label, t: m.timestamp, text: text.length > 240 ? text.slice(0, 237) + '…' : text });
        authors.set(id, a);
        total++;
      }
      after = last;
      if (msgs.length < 100) break;
    }
    if (!windowed && last !== (cursor ?? start)) pending[ch.id] = last;
  }

  if (!windowed) {
    state.pending = { at: new Date().toISOString(), cursors: pending };
    saveState(state);
  }

  // Keep the longest messages per author (they carry the most about the
  // person), shrinking the per-author cap until the digest fits.
  // Humans first; bots after them with a tighter cap (their lines are context).
  const list = [...authors.values()].sort((a, b) => (a.bot - b.bot) || (b.msgs.length - a.msgs.length));
  let voiceBlock = '';
  if (voice.size) {
    voiceBlock = '\n## Voice-aktivitás (napló, nem személy) — ki mikor volt voice-ban\n';
    for (const [id, s] of [...voice.entries()].sort((a, b) => b[1].times.length - a[1].times.length)) {
      const t = s.times.sort();
      voiceBlock += `- <@${id}>: ${s.joined}× be, ${s.left}× ki, ${s.switched}× váltott; ${hhmm(t[0])}–${hhmm(t[t.length - 1])}\n`;
    }
  }
  let perAuthor = 40, out = '';
  for (;;) {
    out = `# Discord digest — ${total} üzenet, ${list.length} szerző, ${channels.length} csatorna, ${guilds.length} szerver\n`;
    for (const a of list) {
      const cap = a.bot ? Math.min(10, perAuthor) : perAuthor;
      const picked = [...a.msgs].sort((x, y) => y.text.length - x.text.length).slice(0, cap)
        .sort((x, y) => (x.t < y.t ? -1 : 1));
      out += a.bot
        ? `\n## (bot) ${a.name} (@${a.user}) — ${a.msgs.length} üzenet, NEM személy\n`
        : `\n## ${a.name} (@${a.user}, <@${a.id}>) — ${a.msgs.length} üzenet\n`;
      for (const m of picked) out += `- [#${m.ch}] ${m.text}\n`;
    }
    out += voiceBlock;
    if (out.length <= maxChars || perAuthor <= 3) break;
    perAuthor = Math.max(3, Math.floor(perAuthor * 0.7));
  }
  if (out.length > maxChars) out = out.slice(0, maxChars) + '\n…(levágva)\n';
  process.stdout.write(out);
}

const headingKey = (line) => {
  const id = line.match(/<@!?(\d+)>/);
  return id ? `id:${id[1]}` : `name:${line.replace(/^##\s+/, '').replace(/\s*\(.*$/, '').trim().toLowerCase()}`;
};
const headings = (s) => s.split('\n').filter((l) => l.startsWith('## ')).map(headingKey);
const nameKey = (line) => `name:${line.replace(/^##\s+/, '').replace(/\s*\(.*$/, '').trim().toLowerCase()}`;
const preamble = (s) => s.split(/\n(?=## )/)[0];
const stamp = () => new Date().toISOString().replace(/[:.]/g, '-');

// Sections and permanent-rule lines of `before` that `after` no longer has.
function losses(before, after) {
  const keys = new Set(headings(after));
  const names = new Set(after.split('\n').filter((l) => l.startsWith('## ')).map(nameKey));
  const lost = before.split('\n').filter((l) => l.startsWith('## '))
    .filter((l) => !keys.has(headingKey(l)) && !names.has(nameKey(l)));
  const lostRules = before.split('\n').filter((l) => /végleges szabály/i.test(l)).filter((l) => !after.includes(l.trim()));
  return { lost, lostRules };
}

function saveLive(text, why) {
  fs.mkdirSync(HISTORY, { recursive: true });
  if (fs.existsSync(LIVE)) fs.copyFileSync(LIVE, path.join(HISTORY, `MEMORY-${stamp()}.md`));
  fs.writeFileSync(LIVE + '.tmp', text);
  fs.renameSync(LIVE + '.tmp', LIVE);
  fs.writeFileSync(LAST_GOOD, text);
  const old = fs.readdirSync(HISTORY).filter((f) => f.startsWith('MEMORY-')).sort().slice(0, -30);
  for (const f of old) fs.rmSync(path.join(HISTORY, f));
  if (why) console.log(why);
}

function heal() {
  const live = fs.existsSync(LIVE) ? fs.readFileSync(LIVE, 'utf8') : '';
  if (!fs.existsSync(LAST_GOOD)) {
    if (live.includes('<!--')) {
      fs.mkdirSync(STATE_DIR, { recursive: true });
      fs.writeFileSync(LAST_GOOD, live);
    }
    return live;
  }
  const good = fs.readFileSync(LAST_GOOD, 'utf8');
  if (live === good) return live;
  const { lost, lostRules } = losses(good, live);
  fs.mkdirSync(HISTORY, { recursive: true });
  if (lost.length || lostRules.length) {
    fs.writeFileSync(path.join(HISTORY, `clobbered-${stamp()}.md`), live);
    fs.writeFileSync(LIVE, good);
    console.log(`HELYREÁLLÍTVA: a people/MEMORY.md-ből eltűnt ${lost.length} szekció / ${lostRules.length} szabálysor (valaki felülírta) — visszaállítva az utolsó jó változatra.`);
    return good;
  }
  if (!live.includes('<!--') && good.includes('<!--')) {
    const body = live.includes('\n## ') || live.startsWith('## ') ? live.slice(live.search(/(^|\n)## /)).replace(/^\n/, '') : '';
    const fixed = preamble(good).replace(/\n*$/, '\n\n') + body;
    fs.writeFileSync(path.join(HISTORY, `clobbered-${stamp()}.md`), live);
    fs.writeFileSync(LIVE, fixed);
    fs.writeFileSync(LAST_GOOD, fixed);
    return fixed;
  }
  // Additions or line edits made some other way: accept them as the new baseline.
  fs.writeFileSync(LAST_GOOD, live);
  return live;
}

// Present-state wording ("épp vonaton ülök", "már otthon van", "egy óra múlva
// ér haza"). Gemma rarely passed --temp, so a newer status sat next to the
// stale one; matching the wording here makes the replacement deterministic.
// --durable overrides a false positive.
const TEMP_RE = /(?<!\p{L})(?:épp(?:en)?|most|jelenleg|pillanatnyilag|ma|ma\s+este|holnap|úton|vonaton|buszon|autóban|otthon|hazaért|hazaér|megjött|megjöttem|megérkezett|nyaral\p{L}*|szabadságon|visszajö\p{L}*|jö\p{L}*\s+vissza|este\s+jö\p{L}*|(?:\d+|egy|két|pár|fél)\s+(?:órá?|perc)\p{L}*\s+múlva)(?!\p{L})/iu;

// Gemma folds two facts into one line ("Szegeden lakik és Rustban
// programozik") despite the one-fact-per-call rule. A correction or forget
// that matches one clause must not take the other with it, so these drop
// only the matching clause and return null when nothing is left.
function dropClause(line, part) {
  const m = line.match(/^(- (?:\(ideiglenes,[^)]*\)\s*)?)(.*)$/);
  const clauses = m[2].split(/,\s*|\s+és\s+|;\s*/).filter(Boolean);
  const keep = clauses.filter((c) => !c.toLowerCase().includes(part));
  if (keep.length === clauses.length) return line;
  return keep.length ? `${m[1]}${keep.join(', ').replace(/\.?$/, '.')}` : null;
}

const budapestDate = () => new Intl.DateTimeFormat('en-CA', { timeZone: 'Europe/Budapest' }).format(new Date());

// [start, end) line range of the person's section, by <@id> first, then by name.
function findSection(lines, id, name) {
  let start = lines.findIndex((l) => l.startsWith('## ') && id && l.includes(`<@${id}>`));
  if (start < 0 && name) start = lines.findIndex((l) => l.startsWith('## ') && nameKey(l) === nameKey(`## ${name}`));
  if (start < 0) return null;
  let end = start + 1;
  while (end < lines.length && !lines[end].startsWith('## ')) end++;
  return [start, end];
}

function personArgs(opt) {
  const id = String(opt.id || '').replace(/[<@!>]/g, '');
  const name = typeof opt.name === 'string' ? opt.name.trim() : '';
  if (!/^\d{5,}$/.test(id) && !name) throw new Error('adj meg --id <discord ID>-t (vagy legalább --name-et)');
  return { id: /^\d{5,}$/.test(id) ? id : '', name };
}

function note(opt) {
  const { id, name } = personArgs(opt);
  let text = typeof opt.text === 'string' ? opt.text.replace(/\s+/g, ' ').trim().replace(/^[-*]\s*/, '') : '';
  if (!text) throw new Error('adj meg --text "<tény>"-t');
  if (text.length > 200) text = text.slice(0, 197) + '…';
  text = text.replace(/^\(ideiglenes,[^)]*\)\s*/, '');
  const temp = opt.temp === true || (opt.durable !== true && TEMP_RE.test(text));
  const lines = heal().replace(/\n*$/, '\n').split('\n');
  lines.pop();
  let sec = findSection(lines, id, name);
  if (!sec) {
    while (lines.length && lines[lines.length - 1] === '') lines.pop();
    lines.push('', `## ${name || id}${id ? ` (<@${id}>)` : ''}`);
    sec = [lines.length - 1, lines.length];
  } else if (id && !lines[sec[0]].includes(`<@${id}>`)) {
    lines[sec[0]] = `${lines[sec[0]]} (<@${id}>)`;
  }
  let [start, end] = sec;
  const body = lines.slice(start + 1, end);
  const replace = typeof opt.replace === 'string' ? opt.replace.trim().toLowerCase() : '';
  const keep = body.flatMap((l) => {
    if (/végleges szabály/i.test(l)) return [l];
    if (temp && /^- \(ideiglenes,/.test(l)) return [];
    if (replace && l.startsWith('- ')) { const r = dropClause(l, replace); return r ? [r] : []; }
    return [l];
  });
  const line = temp ? `- (ideiglenes, ${budapestDate()}) ${text}` : `- ${text}`;
  if (keep.some((l) => l.trim() === line)) { console.log('OK: már benne volt, nincs változás'); return; }
  while (keep.length && keep[keep.length - 1] === '') keep.pop();
  keep.push(line);
  const out = [...lines.slice(0, start + 1), ...keep, ...(end < lines.length ? [''] : []), ...lines.slice(end)];
  saveLive(out.join('\n').replace(/\n{3,}/g, '\n\n').replace(/\n*$/, '\n'),
    `OK: elmentve — ${lines[start].replace(/^##\s+/, '')}: ${line.slice(2)}`);
}

function forget(opt) {
  const { id, name } = personArgs(opt);
  const lines = heal().replace(/\n*$/, '\n').split('\n');
  lines.pop();
  const sec = findSection(lines, id, name);
  if (!sec) { console.log('OK: erről az emberről nincs szekció'); return; }
  const [start, end] = sec;
  let out, removed;
  if (opt.all === true) {
    removed = end - start;
    out = [...lines.slice(0, start), ...lines.slice(end)];
  } else {
    const match = typeof opt.match === 'string' ? opt.match.toLowerCase() : '';
    if (!match) throw new Error('adj meg --match "<részlet>"-et vagy --all-t');
    const body = lines.slice(start + 1, end);
    removed = 0;
    const keep = body.flatMap((l) => {
      if (/végleges szabály/i.test(l) || !l.startsWith('- ')) return [l];
      const r = dropClause(l, match);
      if (r !== l) removed++;
      return r ? [r] : [];
    });
    out = [...lines.slice(0, start + 1), ...keep, ...lines.slice(end)];
  }
  if (!removed) { console.log('OK: nem találtam ilyen sort, nincs változás'); return; }
  saveLive(out.join('\n').replace(/\n{3,}/g, '\n\n').replace(/\n*$/, '\n'), `OK: ${removed} sor törölve/rövidítve`);
}

function apply(opt) {
  heal();
  const state = loadState();
  const commit = () => {
    if (state.pending) {
      state.cursors = { ...state.cursors, ...state.pending.cursors };
      delete state.pending;
      saveState(state);
    }
  };
  if (opt['no-change']) {
    if (fs.existsSync(NEXT)) fs.rmSync(NEXT);
    commit();
    console.log('OK: no change, cursors committed');
    return;
  }
  if (!fs.existsSync(NEXT)) {
    console.log(`REJECTED: ${path.relative(WS, NEXT)} does not exist — write the full new file there first`);
    process.exitCode = 1;
    return;
  }
  const live = fs.existsSync(LIVE) ? fs.readFileSync(LIVE, 'utf8') : '';
  const next = fs.readFileSync(NEXT, 'utf8');
  const nextKeys = new Set(headings(next));
  const nextNames = new Set(next.split('\n').filter((l) => l.startsWith('## ')).map((l) => `name:${l.replace(/^##\s+/, '').replace(/\s*\(.*$/, '').trim().toLowerCase()}`));
  const problems = [];
  const lost = live.split('\n').filter((l) => l.startsWith('## ')).filter((l) => !nextKeys.has(headingKey(l)) && !nextNames.has(`name:${l.replace(/^##\s+/, '').replace(/\s*\(.*$/, '').trim().toLowerCase()}`));
  if (lost.length) problems.push(`eltűnt szekció: ${lost.join(' | ')}`);
  const rules = live.split('\n').filter((l) => /végleges szabály/i.test(l));
  const lostRules = rules.filter((l) => !next.includes(l.trim()));
  if (lostRules.length) problems.push(`végleges szabály sor elveszett: ${lostRules.join(' | ')}`);
  if (live.length > 0 && next.length < live.length * 0.7) problems.push(`túl nagy zsugorodás: ${live.length} → ${next.length} karakter`);
  if (next.length > 12000) problems.push(`túl nagy fájl: ${next.length} karakter (max 12000, cél 6000)`);

  fs.mkdirSync(HISTORY, { recursive: true });
  const ts = stamp();
  if (problems.length) {
    // copy + unlink, not rename: the workspace may be a separate mount
    // from the state dir (EXDEV).
    fs.copyFileSync(NEXT, path.join(HISTORY, `rejected-${ts}.md`));
    fs.rmSync(NEXT);
    console.log(`REJECTED: ${problems.join('; ')}. Az élő fájl változatlan; javítsd a javaslatot és írd újra a MEMORY.next.md-be.`);
    process.exitCode = 1;
    return;
  }
  if (live) fs.writeFileSync(path.join(HISTORY, `MEMORY-${ts}.md`), live);
  fs.renameSync(NEXT, LIVE);
  fs.writeFileSync(LAST_GOOD, next);
  commit();
  // Keep the newest 30 backups.
  const old = fs.readdirSync(HISTORY).filter((f) => f.startsWith('MEMORY-')).sort().slice(0, -30);
  for (const f of old) fs.rmSync(path.join(HISTORY, f));
  console.log(`APPLIED: ${live.length} → ${next.length} karakter, ${headings(live).length} → ${headings(next).length} szekció`);
}

// history: the bot's channel-history search. On 2026-10-19 it read 50-100
// raw messages per `message read` (122-197 KB of JSON, truncated before it
// saw the RuinBot posts), paged with an `offset` the tool does not have, and
// then invented a reason for finding nothing. This walks back with --before,
// keeps embeds (bot posts are often embed-only), filters, and always says how
// far back it looked, so "not found" comes with its real scope.
async function history(opt) {
  const channel = String(opt.channel || '').match(/\d{15,22}/)?.[0];
  if (!channel) throw new Error('history needs --channel <channel id>');
  const limit = Math.min(Number(opt.limit) || 20, 100);
  const pages = Math.min(Number(opt.pages) || 10, 50);
  const cutoff = opt.days !== undefined ? Date.now() - Number(opt.days) * 86400e3 : null;
  const author = opt.author === undefined ? null : String(opt.author).toLowerCase();
  const grep = opt.grep === undefined ? null : String(opt.grep).toLowerCase();
  const self = selfId();
  let before = opt.before ? String(opt.before) : null;
  const hits = [];
  let scanned = 0, oldest = null, newest = null, end = 'page limit';
  outer: for (let page = 0; page < pages; page++) {
    let msgs;
    try { msgs = await readPage(channel, before); } catch (err) { end = `read failed: ${err.message.split('\n')[0]}`; break; }
    if (msgs.length === 0) { end = 'start of channel'; break; }
    msgs.sort((a, b) => (BigInt(a.id) > BigInt(b.id) ? -1 : 1)); // newest first
    for (const m of msgs) {
      if (cutoff !== null && Date.parse(m.timestamp) < cutoff) { end = `--days ${opt.days}`; break outer; }
      scanned++;
      newest ??= m.timestamp;
      oldest = m.timestamp;
      before = m.id;
      const who = m.author?.global_name || m.author?.username || '?';
      if (author && !(m.author?.id === author || who.toLowerCase().includes(author) || String(m.author?.username).toLowerCase().includes(author))) continue;
      const text = messageText(m).replace(/\s+/g, ' ').trim();
      if (grep && !text.toLowerCase().includes(grep)) continue;
      if (!text && !(m.attachments || []).length) continue;
      const tag = m.author?.id === self ? ' (te)' : m.author?.bot ? ' (bot)' : '';
      const att = (m.attachments || []).length ? ` [+${m.attachments.length} csatolmány]` : '';
      hits.push(`${ymd(m.timestamp)} ${hhmm(m.timestamp)} ${who}${tag} <@${m.author?.id}>: ${text.length > 300 ? text.slice(0, 297) + '…' : text}${att} (id ${m.id})`);
      if (hits.length >= limit) { end = `--limit ${limit}`; break outer; }
    }
  }
  const span = scanned ? `${ymd(oldest)} ${hhmm(oldest)} – ${ymd(newest)} ${hhmm(newest)}` : 'nothing';
  console.log(`# history channel ${channel}: ${hits.length} match(es) in ${scanned} messages scanned (${span}); stopped at: ${end}`);
  if (hits.length === 0) console.log('No match in the scanned range. Say exactly that range; to look further back run again with --before ' + (before ?? '<id>') + '.');
  for (const h of hits) console.log(h);
  if (hits.length && before) console.log(`# older: --before ${before}`);
}

const opt = args(process.argv.slice(2));
const cmd = opt._[0];
try {
  if (cmd === 'collect') await collect(opt);
  else if (cmd === 'history') await history(opt);
  else if (cmd === 'apply') apply(opt);
  else if (cmd === 'note') note(opt);
  else if (cmd === 'forget') forget(opt);
  else {
    console.error('usage: people-digest.mjs collect --guild <id>[,<id>…]|all [--nsfw] [--hours 24 | --from <date> [--to <date>] | --last-days N] [--exclude a,b] [--max-chars 60000] | apply [--no-change]\n' +
      '       | note --id <id> --name <name> --text <fact> [--temp|--durable] [--replace <part>] | forget --id <id> (--match <part> | --all)\n' +
      '       | history --channel <id> [--author <name|id>] [--grep <text>] [--limit 20] [--days N] [--pages 10] [--before <msg id>]');
    process.exitCode = 2;
  }
} catch (err) {
  console.error(`people-digest: ${err.message}`);
  process.exitCode = 1;
}
