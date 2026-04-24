# Discord voice-controlled agent — internals + design notes

Deeper reference for the end-user runbook at [`docs/CUSTOMIZATION.md` → "Voice-controlled agent over Discord"](../CUSTOMIZATION.md#voice-controlled-agent-over-discord). This doc covers the schema we've observed on the live gateway, the isolation design and *why* it's wired the way it is, the threat model, and the known gaps.

## Flow

```
User (Discord voice channel)
   │  Opus-encoded audio over UDP
   ▼
Discord voice servers
   │  voice receive stream (Opus 48kHz stereo)
   ▼
openclaw-gateway (our container, via the bot gateway websocket)
   │  VAD-chunked utterances, re-encoded to wav via bundled ffmpeg
   ▼  POST multipart /v1/audio/transcriptions
openclaw-stt-whisper (Trendency/whisper-large-v3-hu, faster-whisper, float16)
   │  transcript
   ▼
openclaw-gateway (agent dispatch — routed to the bound `discord-voice` agent)
   │  isolated workspace at ~/.openclaw/workspace-discord/
   │  tool calls subject to cautious exec-policy (destructive ops approval-gated)
   ▼
vllm-llm (Gemma 4 31B NVFP4) + tool runtime
   │  final assistant message
   ▼  POST /v1/audio/speech (router → Kokoro EN / F5-TTS HU based on diacritics)
openclaw-tts-router → openclaw-tts-en / openclaw-tts-f5hun
   │  Opus stream
   ▼
Discord voice servers → user hears the reply
```

Every stage is a pre-existing service in this stack. The Discord integration itself is 100% configuration — no custom code, no new container.

## Schema: `channels.discord.*`

Written by `openclaw channels add --channel discord --token <token>` and tuned via `openclaw channels configure`. Observed on OpenClaw ≥ 2026.4.15:

```json5
{
  "channels": {
    "discord": {
      "enabled": true,
      "auth": {
        "token": "<bot-token>"         // store reads through gateway.authTokenStore
      },
      "voice": {
        "enabled": true,                // default true when the discord channel is added
        "daveEncryption": true,         // Discord DAVE E2E voice protocol
        "decryptionFailureTolerance": 10,
        "autoJoin": [
          { "guild": "<guild-snowflake>", "channel": "<voice-channel-snowflake>" }
        ],
        "vad": {
          "enabled": true,
          "silenceTimeoutMs": 800,      // end-of-utterance threshold
          "interruptOnSpeech": false
        },
        "tts": {
          // Optional override; defaults to the gateway-level messages.tts wiring
          // (patcher step 11). Setting voiceId here pins a specific voice for
          // this channel — useful if the operator wants Hungarian in one guild
          // and English in another.
          "voiceId": "af_heart"
        }
      }
    }
  }
}
```

**Why not write this from `patch-config.mjs`?** Two reasons:

1. The token is secret-grade and lives in OpenClaw's credential store (`gateway.authTokenStore`), not the plaintext config file. `openclaw channels add` is the only supported path that plumbs the secret through the right store. Writing it to `openclaw.json` directly would leak it into log scrapes and backups.
2. The exact leaf field names vary slightly between minor OpenClaw releases (we've seen `auth.token` and `bot.token` in the wild). Letting the CLI do the write decouples us from schema drift.

If the schema stabilizes across a couple of OpenClaw minor releases, a future patcher step 15 could take over the re-upsert of non-secret fields (voice.enabled, voice.autoJoin, vad.*) while leaving auth.token to the CLI.

## Isolation design

The runbook's isolation goal: **anyone who can speak in the bound Discord voice channel should not be able to reach memory notes or tools scoped to the operator's primary workspace**. Three layers enforce this, each independently sufficient for the most common attack paths:

1. **Separate agent.** `openclaw agents add discord-voice --workspace /home/node/.openclaw/workspace-discord` creates a new agent directory under `~/.openclaw/agents/discord-voice/`. The gateway routes voice-channel traffic to this agent (via `openclaw agents bind --agent discord-voice --channel discord`), and the agent's tool calls resolve paths relative to its own workspace — not the operator's primary one.

2. **Disjoint workspace directory.** `~/.openclaw/workspace/` (primary) and `~/.openclaw/workspace-discord/` (Discord) share a parent via the `OPENCLAW_CONFIG_DIR` bind mount, but they're siblings — neither is a subpath of the other. The filesystem-tool sandbox rejects paths containing `..` that escape the agent's configured workspace.

3. **Tightened exec-policy.** `openclaw exec-policy preset --agent discord-voice cautious` routes destructive tool calls (`execute_command`, `write_file` against paths not already present, `delete_file`) through an approval channel. Approvals show up in the bound text channel — so you'd have to explicitly react/reply to authorize a destructive action. `yolo` preset would skip approvals; `deny-all` would disable execution entirely (read-only, still useful for Q&A over workspace docs).

**What the isolation does NOT protect against:**

- **LLM response inference**: the Discord agent can call the same LLM that your primary agent uses. If your LLM system prompt contains personal information (it shouldn't — patcher keeps defaults), that leaks. We enforce a distinct system prompt in the runbook's `openclaw agents add ... --system-prompt "…You have no access to the operator's personal workspace or memory…"`.
- **Cloud LLM leaks**: if you've configured a cloud LLM backend, voice transcripts transit that provider. Run local vLLM (the default) to keep voice data on-prem.
- **Shared SearxNG**: the Discord agent can call `web_search` just like your primary agent. Not a leak per se (web search is stateless), but it does contribute to your shared SearxNG usage quota / logs.
- **Exec-policy bypass**: `yolo` preset defeats the approval gate. If you set it explicitly, you're trusting everyone who can speak in the channel not to abuse it.

## Threat model

| Threat | Mitigation | Residual risk |
|---|---|---|
| Bot token leak → impersonation in all joined guilds | Rotate token immediately (runbook). Store `.env` outside the repo. | An attacker with the token can join + listen + respond as your bot until rotation — there's no revocation of already-captured audio. |
| Malicious voice-channel participant extracts memory | Separate workspace + cautious exec-policy + workspace-scoped fs tools | Transcripts of the attacker's own utterances land in `workspace-discord/memory` if the agent chose to save them. Not a leak of YOUR data, but an inbound-content-control problem. Reviewing `workspace-discord/` periodically is cheap. |
| LLM jailbreak → agent ignores the isolation system prompt | Exec-policy gates, not system-prompt trust. cautious preset means no destructive action without approval. | A determined jailbreak could still exfiltrate the content of the Discord workspace via web_search / text response. Don't put real secrets in the Discord workspace. |
| Guild admin reassigns bot permissions maliciously | Discord permission model is enforced server-side; our bot has `Connect`, `Speak`, `Send Messages`, etc. scoped to the invite. An admin can remove these but can't grant the bot MORE privileges without re-inviting it. | Low; guild admins trust model is a Discord concern. |
| Audio replay / recording by other channel members | None at our end (they were in the channel and heard everything anyway) | Accept it. Document in guild rules that voice-bot transcripts may be logged to the on-prem workspace. |
| GDPR / data-subject request for voice transcripts | `rm ~/.openclaw/workspace-discord/memory/<date-prefix>-*.md` or more targeted grep + delete | Operator must actually respect DSARs. The isolation doesn't help with this; the per-channel workspace at least makes the data-at-rest easy to locate. |

## DAVE encryption

Discord rolled out DAVE (Direct Audio Voice Encryption, an E2EE protocol for voice channels) in 2024. OpenClaw 2026.4.15+ speaks the DAVE protocol (`voice.daveEncryption: true`). This means:

- The voice stream between client and Discord's SFU is E2E-encrypted at the user pairwise level.
- Our bot is a participant in the DAVE key exchange — it holds the shared group key, so it can decrypt what it's there to listen to.
- **DAVE does NOT protect against our bot**. Anyone with access to the bot's process can read plaintext. DAVE protects against Discord itself (and against on-path network attackers who aren't in the DAVE group).

`voice.decryptionFailureTolerance: 10` means the bot will tolerate up to 10 consecutive failed DAVE frame decryptions before dropping the voice session — usually a network hiccup, occasionally an out-of-band key rotation race. Raising it papers over legitimate issues; lowering it makes transient LAN jitter disconnect the bot. 10 is a sensible default.

## Operational gotchas we've hit or expect

- **Token-leak in logs**: earlier OpenClaw versions logged the full bot token in `channels add` debug output. Check `docker logs` with `grep -iE 'token|auth'` after an upgrade; if a token appears, rotate immediately.
- **`/vc join` requires the user to be in a voice channel first**. The bot can't join a voice channel the invoking user isn't already in.
- **Privileged intents**: if the Discord Developer Portal doesn't have `Server Members Intent` enabled, the bot can't match guild members to the voice speakers. The STT still works but multi-speaker context ("Alice just said X, reply to her") won't.
- **Auto-join race on gateway restart**: the `autoJoin` list fires ~5-10 s after gateway startup. If you run `/vc leave` and restart within that window, the bot may re-join faster than you'd expect.
- **Audio codec mismatch**: Discord is Opus 48kHz stereo; Whisper wants 16kHz mono wav. The gateway uses its bundled ffmpeg for this. If ffmpeg is missing (unusual on the OpenClaw image), you'll see `ffmpeg: not found` in the log and voice transcription silently drops. Probe with `docker exec openclaw-gateway ffmpeg -version`.
- **Latency cliff on the first message after idle**: if vLLM was idle and offloaded the KV cache, first utterance after a gap of minutes can take an extra 2-3 s for prefill warm-up. Subsequent utterances are fast.

## Related docs

- End-user runbook: [`docs/CUSTOMIZATION.md` → "Voice-controlled agent over Discord"](../CUSTOMIZATION.md#voice-controlled-agent-over-discord)
- STT service details: [`docs/reference/stt-stack.md`](./stt-stack.md)
- TTS router + voice aliases: [`docs/reference/tts-stack.md`](./tts-stack.md)
- Agent credential stores: [`docs/reference/openclaw-internals.md`](./openclaw-internals.md)
- Upstream OpenClaw channel docs (authoritative): <https://docs.openclaw.ai/channels/discord>
