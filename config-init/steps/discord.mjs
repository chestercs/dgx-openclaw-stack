// Discord channel and command settings: who may talk to the bot and run
// commands, how replies are delivered, and the gates per guild/channel.
// Settings of the Discord-routed agent itself live in steps/agents.mjs.
//
// Nothing here runs until onboarding (`openclaw channels add --channel
// discord`) has created an enabled channels.discord block; the bot token and
// accounts there belong to the operator and are never touched.

import * as env from '../lib/env.mjs';

const D = ['channels', 'discord'];
const SNOWFLAKE = /^\d{17,20}$/;

// "*" or a comma list. Snowflakes stay strings: a 19-digit ID written as a
// JSON number rounds, and the sender check then fails for that user.
const idList = (name) => env.list(name, ['*']);

// First list whose entries are all concrete snowflakes; "*" never qualifies.
function concreteIds(...names) {
  for (const name of names) {
    const v = env.list(name);
    if (v.length && v.every((s) => SNOWFLAKE.test(s))) return v;
  }
  return [];
}

// "Handle=snowflake,Other Handle=snowflake" -> [[handle, id], ...]
function aliasPairs(name) {
  const pairs = env.list(name).map((p) => {
    const eq = p.indexOf('=');
    const handle = p.slice(0, eq).trim();
    const id = p.slice(eq + 1).trim();
    return eq > 0 && handle && SNOWFLAKE.test(id) ? [handle, id] : null;
  });
  if (pairs.includes(null)) console.warn(`[patch-config] ${name}: malformed pairs skipped (expected Handle=<snowflake>).`);
  return pairs.filter(Boolean);
}

export default function discord({ config }) {
  // The built-in video_generate calls OpenAI Sora, which 401s without an
  // OpenAI key (one "make a video" turn called it 16 times). Video goes
  // through comfyui_image__generate_video instead. A global deny removes a
  // tool on every route, guild routes included (deny is checked first).
  if (env.bool('OPENCLAW_DENY_OPENAI_VIDEO_TOOL', true)) config.addTo('tools.deny', ['video_generate'], 'needs an OpenAI key');
  else config.removeFrom('tools.deny', ['video_generate']);
  const guildTools = config.get([...D, 'guilds', '*', 'tools']);
  const why = 'the global deny already covers guild routes';
  if (JSON.stringify(guildTools) === '{"deny":["video_generate"]}') config.remove([...D, 'guilds', '*', 'tools'], why);
  else config.removeFrom([...D, 'guilds', '*', 'tools', 'deny'], ['video_generate'], why);

  // `!<cmd>` runs a shell in the gateway for anyone in ownerAllowFrom; its
  // only use (the `!img` directive) is gone, so the shell stays closed.
  config.remove('commands.bash', 'no shell directive on Discord');

  if (config.get([...D, 'enabled']) !== true) return;

  // Without an explicit false the plugin sends every message with the
  // suppress_embeds flag, so no image/video/link URL unfurls.
  config.set([...D, 'suppressEmbeds'], env.bool('OPENCLAW_DISCORD_SUPPRESS_EMBEDS', false));

  // Slash-command and elevated-tool authz. commands.allowFrom.discord is the
  // guild slash gate (unset = every guild slash command is "not authorized").
  // Owners are written `discord:<id>`: a bare snowflake reads as a channel
  // target, and the heartbeat then finds no owner DM (reason=no-route).
  config.set('commands.allowFrom.discord', idList('OPENCLAW_DISCORD_COMMANDS_ALLOW'));
  config.set('commands.ownerAllowFrom', idList('OPENCLAW_DISCORD_COMMAND_OWNERS').map((id) => (/^\d+$/.test(id) ? `discord:${id}` : id)));
  config.set('tools.elevated.enabled', true);
  config.set('tools.elevated.allowFrom.discord', idList('OPENCLAW_TOOLS_ELEVATED_DISCORD_ALLOW'));

  // Auto-ack reactions off: a stale reaction queue (openclaw #46024) replays
  // them on session resume and the bot cycles emojis on a message. The agent
  // still reacts on purpose through the reaction action below.
  config.setDefault([...D, 'ackReactionScope'], env.str('OPENCLAW_DISCORD_ACK_REACTION_SCOPE', 'off'));
  config.set([...D, 'actions', 'reactions'], env.bool('OPENCLAW_DISCORD_ACTIONS_REACTIONS', true));

  // Progressive delivery: a placeholder edited in place as tokens arrive.
  // Posted atomically, a long answer is a minute of silence that reads as a
  // frozen bot. Discord allows 5 edits / 5 s per channel. `progress` is the
  // Discord alias of `partial`. toolProgress = the "Working... tool: x" lines.
  config.set([...D, 'streaming', 'mode'], env.oneOf('OPENCLAW_DISCORD_STREAMING', ['off', 'partial', 'block', 'progress'], 'partial'));
  const toolProgress = env.bool('OPENCLAW_DISCORD_STREAMING_PREVIEW_TOOL_PROGRESS');
  if (toolProgress !== undefined) config.set([...D, 'streaming', 'preview', 'toolProgress'], toolProgress);

  // A reply is split at 2000 chars or maxLinesPerMessage (default 17) lines;
  // at 17 a code answer came out as six messages with its blocks cut apart.
  // 60 lets the 2000-char limit decide. `off` leaves the key alone.
  if (env.str('OPENCLAW_DISCORD_MAX_LINES', '').toLowerCase() !== 'off') {
    config.set([...D, 'maxLinesPerMessage'], env.int('OPENCLAW_DISCORD_MAX_LINES', 60));
  }

  // Open-guild authz: guild members can use slash commands, which Discord
  // acks at once (no dead air while the model prefills). Upstream leaves the
  // guild allowlist empty, so slash commands fail there (openclaw #19310).
  //   open | pairing (DM strangers need owner approval) | owner-only
  //   (OPENCLAW_DISCORD_OWNER_IDS) | allowlist (leave upstream defaults).
  const authz = env.oneOf('OPENCLAW_DISCORD_AUTHZ', ['open', 'pairing', 'owner-only', 'allowlist'], 'open');
  const owners = env.list('OPENCLAW_DISCORD_OWNER_IDS');
  if (authz === 'open') config.merge(D, { allowFrom: ['*'], dmPolicy: 'open', groupPolicy: 'open' });
  // groupPolicy has no "pairing"; the handshake does not work in group DMs.
  else if (authz === 'pairing') config.merge(D, { allowFrom: ['*'], dmPolicy: 'pairing', groupPolicy: 'disabled' });
  else if (authz === 'owner-only') {
    if (!owners.length || !owners.every((s) => SNOWFLAKE.test(s))) {
      console.warn('[patch-config] OPENCLAW_DISCORD_AUTHZ=owner-only needs OPENCLAW_DISCORD_OWNER_IDS snowflakes; authz left as is (it would lock everyone out).');
    } else config.merge(D, { allowFrom: owners, dmPolicy: 'allowlist', groupPolicy: 'allowlist' });
  }

  // Voice (/vc join|leave|status). With no voice.realtime block the plugin
  // uses batch STT + TTS, i.e. this stack's Whisper and Fish services; the
  // realtime modes need provider credentials the stack does not ship.
  if (env.oneOf('OPENCLAW_DISCORD_VOICE', ['stt-tts', 'off'], 'stt-tts') === 'stt-tts') {
    config.setDefault([...D, 'voice', 'enabled'], true);
  }

  // Thread bindings (/focus, /unfocus, /agents, /session): a thread stays on
  // one session. Upstream unfocuses after 24 h idle and never expires a
  // binding; coding threads are revisited the next day.
  if (env.bool('OPENCLAW_DISCORD_THREAD_BINDINGS', true)) {
    config.setDefault([...D, 'threadBindings', 'enabled'], true);
    for (const [name, key] of [['OPENCLAW_DISCORD_THREAD_IDLE_HOURS', 'idleHours'], ['OPENCLAW_DISCORD_THREAD_MAX_AGE_HOURS', 'maxAgeHours']]) {
      const n = env.int(name);
      if (n !== undefined) config.set([...D, 'threadBindings', key], n);
    }
  }

  // The mention GATE for every guild: on = only @mentions and replies to the
  // bot get through. Written every run because a drifted value is invisible
  // (`/activation always` is only a prompt hint and cannot open a closed
  // gate). guilds.<id> entries win over "*" and stay the operator's.
  const gate = env.bool('OPENCLAW_DISCORD_REQUIRE_MENTION', true);
  config.set([...D, 'guilds', '*', 'requireMention'], gate);

  // Channels where the bot reads everything despite a closed gate. Once a
  // guild entry has a channels map, an UNLISTED channel resolves to
  // allowed:false (silence, mentions included), so a "*" fallback that
  // mirrors the gate is always written with it. `off` removes the map.
  const always = env.raw('OPENCLAW_DISCORD_ALWAYS_CHANNELS');
  if (always?.toLowerCase() === 'off') {
    config.remove([...D, 'guilds', '*', 'channels'], 'OPENCLAW_DISCORD_ALWAYS_CHANNELS=off');
  } else if (always) {
    const chans = env.list('OPENCLAW_DISCORD_ALWAYS_CHANNELS').filter((s) => SNOWFLAKE.test(s));
    if (chans.length) {
      const c = [...D, 'guilds', '*', 'channels'];
      config.set([...c, '*'], { requireMention: gate }, 'unlisted channels would go silent without it');
      for (const id of chans) config.set([...c, id, 'requireMention'], false, 'always-on channel');
    }
  }

  // Exec approval prompts as Discord DMs with buttons; without it a prompt
  // waits on a UI nobody watches and times out to deny. Approvers are never
  // "*": anyone could then approve the bot's own escalation.
  const approvals = env.bool('OPENCLAW_DISCORD_EXEC_APPROVALS', true) && env.bool('OPENCLAW_TOOLS_EXEC', true);
  if (!approvals) config.remove([...D, 'execApprovals']);
  else {
    const approvers = concreteIds('OPENCLAW_EXEC_APPROVERS', 'OPENCLAW_DISCORD_OWNER_IDS', 'OPENCLAW_DISCORD_COMMAND_OWNERS');
    if (approvers.length) config.merge([...D, 'execApprovals'], { enabled: true, approvers, target: 'dm' });
    else console.warn('[patch-config] no approver snowflakes (OPENCLAW_EXEC_APPROVERS); exec approval prompts will not reach Discord.');
  }

  // Presence mirrors runtime health, so a degraded stack shows in the member
  // list before anyone spends a prompt on it.
  if (env.bool('OPENCLAW_DISCORD_AUTO_PRESENCE', true)) {
    config.set([...D, 'autoPresence', 'enabled'], true);
    for (const key of ['healthy', 'degraded', 'exhausted']) {
      const text = env.str(`OPENCLAW_DISCORD_AUTO_PRESENCE_${key.toUpperCase()}_TEXT`);
      if (text) config.set([...D, 'autoPresence', `${key}Text`], text);
    }
  } else config.remove([...D, 'autoPresence']);

  // "first": the first message of a run is a native reply to the trigger, so
  // in a busy channel it is clear which question an answer belongs to.
  config.set([...D, 'replyToMode'], env.oneOf('OPENCLAW_DISCORD_REPLY_TO_MODE', ['off', 'first', 'all', 'batched'], 'first'));

  // Outbound `@handle` becomes a real mention via this map first, then via an
  // in-memory directory of users seen since boot, which is empty right after
  // a restart. Additive: entries added elsewhere stay. `off` removes the map.
  if (env.str('OPENCLAW_DISCORD_MENTION_ALIASES', '').toLowerCase() === 'off') config.remove([...D, 'mentionAliases']);
  else for (const [handle, id] of aliasPairs('OPENCLAW_DISCORD_MENTION_ALIASES')) config.set([...D, 'mentionAliases', handle], id);

  // Bot-authored messages are dropped upstream. "mentions" lets other bots in
  // when they tag this one; the plugin's botLoopProtection then caps a loop.
  const allowBots = env.oneOf('OPENCLAW_DISCORD_ALLOW_BOTS', ['mentions', 'true', 'false', 'off']);
  if (allowBots === 'off') config.remove([...D, 'allowBots']);
  else if (allowBots) config.set([...D, 'allowBots'], allowBots === 'mentions' ? 'mentions' : allowBots === 'true');

  // Target for sends that name none. Without it, cron and heartbeat turns
  // invented targets (channel:bot-channel, a placeholder id). `off` removes.
  const defaultTo = env.raw('OPENCLAW_DISCORD_DEFAULT_TO');
  if (defaultTo?.toLowerCase() === 'off') config.remove([...D, 'defaultTo']);
  else if (defaultTo && /^(channel|user):\d{5,}$/.test(defaultTo)) config.set([...D, 'defaultTo'], defaultTo);
  else if (defaultTo) console.warn(`[patch-config] OPENCLAW_DISCORD_DEFAULT_TO=${defaultTo} is not channel:<id> or user:<id>; skipped.`);
}
