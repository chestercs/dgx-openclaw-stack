// Workspace files the patcher owns: marker blocks inside a shared file
// (AGENTS.md is shared with the operator), whole skill files, and the
// templates their text comes from (config-init/templates/).

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const TEMPLATES = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', 'templates');
const log = (msg) => console.log(`[patch-config] ${msg}`);
const short = (file) => file.replace('/home/node/.openclaw/', '');

// Template text with {{NAME}} placeholders. A placeholder without a value is
// an error: a half-rendered prompt is worse than a failed init.
export function template(rel, vars = {}) {
  const text = fs.readFileSync(path.join(TEMPLATES, rel), 'utf8');
  return text.replace(/\{\{(\w+)\}\}/g, (_, k) => {
    if (vars[k] === undefined) throw new Error(`template ${rel}: no value for {{${k}}}`);
    return String(vars[k]);
  });
}

export function writeIfChanged(file, content, mode) {
  const current = fs.existsSync(file) ? fs.readFileSync(file, 'utf8') : null;
  if (current === content) return false;
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, content, mode ? { mode } : undefined);
  log(`${short(file)} ${current === null ? 'created' : 'updated'}`);
  return true;
}

export function removeFile(file) {
  if (!fs.existsSync(file)) return false;
  fs.unlinkSync(file);
  try { fs.rmdirSync(path.dirname(file)); } catch { /* the folder holds other files */ }
  log(`${short(file)} removed`);
  return true;
}

const markers = (id) => [`<!-- patch-config:${id}:start -->`, `<!-- patch-config:${id}:end -->`];

// Puts `body` between the block's markers, appending the block when it is
// missing. Text outside the markers belongs to the operator and is kept.
export function upsertBlock(file, id, body) {
  const [start, end] = markers(id);
  const content = fs.existsSync(file) ? fs.readFileSync(file, 'utf8') : '';
  const s = content.indexOf(start);
  const e = content.indexOf(end);
  let next;
  if (s === -1 || e === -1 || e < s) {
    const sep = content === '' || content.endsWith('\n') ? '' : '\n';
    next = `${content}${sep}${content === '' ? '' : '\n'}${start}\n${body}${end}\n`;
  } else {
    next = content.slice(0, s + start.length) + `\n${body}` + content.slice(e);
  }
  if (next === content) return false;
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, next);
  log(`${short(file)}: block ${id} ${s === -1 ? 'added' : 'updated'}`);
  return true;
}

export function removeBlock(file, id) {
  const [start, end] = markers(id);
  if (!fs.existsSync(file)) return false;
  const content = fs.readFileSync(file, 'utf8');
  const s = content.indexOf(start);
  const e = content.indexOf(end);
  if (s === -1 || e === -1 || e < s) return false;
  let from = s;
  let to = e + end.length;
  if (content[to] === '\n') to += 1;
  if (from > 0 && content[from - 1] === '\n') from -= 1;
  fs.writeFileSync(file, content.slice(0, from) + content.slice(to));
  log(`${short(file)}: block ${id} removed`);
  return true;
}

// A workspace skill (skills/<name>/SKILL.md) is wholly patcher-owned.
export function skill(workspace, name, description, body) {
  const content =
    `---\nname: ${name}\ndescription: ${JSON.stringify(description)}\n---\n\n` +
    '<!-- managed by config-init; edits are overwritten on every compose up -->\n' +
    body;
  return writeIfChanged(path.join(workspace, 'skills', name, 'SKILL.md'), content);
}

export const removeSkill = (workspace, name) => removeFile(path.join(workspace, 'skills', name, 'SKILL.md'));
