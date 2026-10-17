#!/usr/bin/env node
// people-digest — feeds the nightly people-memory job of the Discord agent.
//
// The job (an `openclaw cron` agent turn, see the `people-memory` workspace
// skill written by patch-config.mjs step 44) keeps people/MEMORY.md up to
// date from what guild members say in ordinary channel traffic, not only
// from what they tell the bot directly. Two subcommands:
//
//   collect --guild <id> [--hours 24] [--exclude a,b] [--max-chars 60000]
//     Reads every text channel of the guild since the last committed cursor
//     (first run: the last --hours) through `openclaw message read`, drops
//     bot authors, NSFW channels, *log channels and --exclude names, and
//     prints a compact per-author digest. A 26B model calling `message read`
//     thirty times itself was slow and lost track; one pre-digested block is
//     cheaper and deterministic.
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
//   note --id <discord id> --name <name> --text <fact> [--temp] [--replace <part>]
//   forget --id <discord id> (--match <part> | --all)
//     The bot's "remember this" path. Gemma editing the shared file itself
//     failed twice: an `edit` anchored on a section that did not exist yet,
//     then a fallback `write` that replaced the whole file. These commands
//     find or create the person's section and change one line. --temp stamps
//     a dated `(ideiglenes, YYYY-MM-DD)` line and replaces the person's older
//     temporary line.
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

const snowflakeAt = (ms) => ((BigInt(ms) - DISCORD_EPOCH) << 22n).toString();
const maxId = (a, b) => (BigInt(a) > BigInt(b) ? a : b);

function collect(opt) {
  heal();
  const guild = String(opt.guild || process.env.PEOPLE_DIGEST_GUILD || '');
  if (!/^\d{5,}$/.test(guild)) throw new Error('collect needs --guild <id> (or PEOPLE_DIGEST_GUILD)');
  const hours = Math.min(Number(opt.hours) || 24, MAX_LOOKBACK_HOURS);
  const maxChars = Number(opt['max-chars']) || 60000;
  const exclude = new Set(String(opt.exclude || '').split(',').map((s) => s.trim()).filter(Boolean));
  const state = loadState();
  const floor = snowflakeAt(Date.now() - MAX_LOOKBACK_HOURS * 3600e3);
  const start = snowflakeAt(Date.now() - hours * 3600e3);

  const channels = (firstArray(openclaw(['message', 'channel', 'list', '--channel', 'discord', '--guild-id', guild])) || [])
    .filter((c) => (c.type === 0 || c.type === 5) && !c.nsfw && !exclude.has(c.name) && !/(^|-)log$/.test(c.name));

  const authors = new Map();
  const pending = {};
  let total = 0;
  for (const ch of channels) {
    const cursor = state.cursors[ch.id];
    let after = cursor ? maxId(cursor, floor) : start;
    let last = after;
    for (let page = 0; page < 5; page++) {
      let msgs;
      try {
        msgs = firstArray(openclaw(['message', 'read', '--channel', 'discord', '--target', `channel:${ch.id}`, '--after', after, '--limit', '100'])) || [];
      } catch {
        break; // missing permission / deleted channel: skip, keep old cursor
      }
      if (msgs.length === 0) break;
      msgs.sort((a, b) => (BigInt(a.id) < BigInt(b.id) ? -1 : 1));
      for (const m of msgs) {
        last = maxId(last, m.id);
        if (m.author?.bot) continue;
        const text = String(m.content || '').replace(/\s+/g, ' ').trim();
        if (!text || /^(https?:\/\/\S+\s*)+$/.test(text)) continue;
        const id = m.author.id;
        const a = authors.get(id) ?? { id, name: m.author.global_name || m.author.username, user: m.author.username, msgs: [] };
        a.msgs.push({ ch: ch.name, t: m.timestamp, text: text.length > 240 ? text.slice(0, 237) + '…' : text });
        authors.set(id, a);
        total++;
      }
      after = last;
      if (msgs.length < 100) break;
    }
    if (last !== (cursor ?? start)) pending[ch.id] = last;
  }

  state.pending = { at: new Date().toISOString(), cursors: pending };
  saveState(state);

  // Keep the longest messages per author (they carry the most about the
  // person), shrinking the per-author cap until the digest fits.
  const list = [...authors.values()].sort((a, b) => b.msgs.length - a.msgs.length);
  let perAuthor = 40, out = '';
  for (;;) {
    out = `# Discord digest — ${total} üzenet, ${list.length} szerző, ${channels.length} csatorna\n`;
    for (const a of list) {
      const picked = [...a.msgs].sort((x, y) => y.text.length - x.text.length).slice(0, perAuthor)
        .sort((x, y) => (x.t < y.t ? -1 : 1));
      out += `\n## ${a.name} (@${a.user}, <@${a.id}>) — ${a.msgs.length} üzenet\n`;
      for (const m of picked) out += `- [#${m.ch}] ${m.text}\n`;
    }
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
  const keep = body.filter((l) => {
    if (/végleges szabály/i.test(l)) return true;
    if (opt.temp && /^- \(ideiglenes,/.test(l)) return false;
    if (typeof opt.replace === 'string' && opt.replace && l.toLowerCase().includes(opt.replace.toLowerCase())) return false;
    return true;
  });
  const line = opt.temp ? `- (ideiglenes, ${budapestDate()}) ${text}` : `- ${text}`;
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
    const keep = body.filter((l) => /végleges szabály/i.test(l) || !l.toLowerCase().includes(match));
    removed = body.length - keep.length;
    out = [...lines.slice(0, start + 1), ...keep, ...lines.slice(end)];
  }
  if (!removed) { console.log('OK: nem találtam ilyen sort, nincs változás'); return; }
  saveLive(out.join('\n').replace(/\n{3,}/g, '\n\n').replace(/\n*$/, '\n'), `OK: ${removed} sor törölve`);
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

const opt = args(process.argv.slice(2));
const cmd = opt._[0];
try {
  if (cmd === 'collect') collect(opt);
  else if (cmd === 'apply') apply(opt);
  else if (cmd === 'note') note(opt);
  else if (cmd === 'forget') forget(opt);
  else {
    console.error('usage: people-digest.mjs collect --guild <id> [--hours 24] [--exclude a,b] [--max-chars 60000] | apply [--no-change]\n' +
      '       | note --id <id> --name <name> --text <fact> [--temp] [--replace <part>] | forget --id <id> (--match <part> | --all)');
    process.exitCode = 2;
  }
} catch (err) {
  console.error(`people-digest: ${err.message}`);
  process.exitCode = 1;
}
