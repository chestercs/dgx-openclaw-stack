// harness-guard — policy checks outside the model for the Discord agent:
// the model proposes a tool call, this hook decides whether it may run.
//
// Owner-only tools in DMs. Guild channels get the same rule from the
// official channels.discord.guilds["*"].toolsBySender policy (config-init),
// which also takes the tools out of the prompt. Discord has no per-sender
// tool policy for DMs, and the agent-level toolsBySender would also hit
// scheduled runs (they have no sender and match "*"), which need exec. So in
// a DM the peer in the session key (…:discord:direct:<id>) must be an owner
// for these tools to run; runs without a sender pass.
//
// The block reason tells the model what to say instead of retrying.
import { definePluginEntry } from 'openclaw/plugin-sdk/plugin-entry';

const ids = (v) => (Array.isArray(v) ? v : []).map((x) => String(x).match(/\d{15,22}/)?.[0]).filter(Boolean);

export default definePluginEntry({
  id: 'harness-guard',
  name: 'Harness Guard',
  description: 'Policy checks outside the model: owner-only tools in Discord DMs.',
  register(api) {
    const pc = api.pluginConfig ?? {};
    const agents = new Set(pc.agents ?? []);
    const owners = new Set(ids(pc.owners));
    const ownerOnly = new Set(pc.ownerOnlyTools ?? []);

    api.on('before_tool_call', (event, ctx) => {
      if (!agents.has(ctx?.agentId) || !ownerOnly.has(event?.toolName)) return;
      const peer = String(ctx?.sessionKey ?? '').match(/:discord:direct:(\d{15,22})/)?.[1];
      if (!peer || owners.has(peer)) return;
      return {
        block: true,
        blockReason: `${event.toolName} runs only for the bot's owner. Do not retry: tell the user plainly that this is not available to them, and offer what you can do instead.`,
      };
    });
  },
});
