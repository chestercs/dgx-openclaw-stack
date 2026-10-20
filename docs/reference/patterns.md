# Reusable Docker / dev patterns

> Reference material: general lessons worth carrying to other projects.

## Cross-compose service discovery — `name:` + auto-attach

When you need service discovery across separate Docker Compose stacks (cross-compose DNS), the stable pattern is:

```yaml
networks:
  shared-net:
    name: shared-net      # explicit network name (NOT project-prefixed)
    driver: bridge
    # NO `external: true` — auto-create
```

Every compose file carries the same block. The FIRST `compose up` (whichever runs first) creates the network; every other stack sees it and just attaches. On `down`, only the "owner" project (the one that created it) deletes the network, tracked by Docker labels — while any stack is still up, the network stays.

### Why

`compose up` should create the network if it does not exist and attach to it if it does, with no separate `docker network create` step. The `external: true` pattern does not do that (you must `docker network create` it the first time); `external: false` + `name:` does.

This stack's own optional ComfyUI bridge (`openclaw-image-comfyui/docker-compose.yml`) takes the simpler variant: it joins the main stack's default network (`${COMPOSE_PROJECT_NAME}_default`) with `external: true`, because the main stack is always up first.

### How to apply

- Use this as the default for any new cross-compose setup.
- Services should reference each other by hostname (`vllm-embedding:8005`, `vllm-llm:8004`), NOT via `host.docker.internal` / IPs / `extra_hosts`. Cross-platform stable (Linux daemon, Docker Desktop on Mac / Windows).
- The `name:` field is CRITICAL — without it, Compose creates a `<project>_<networkname>` form and the two projects see different networks.
- If a service also needs another internal network (e.g., a private DB), pass a list: `networks: [shared-net, internal-net]`.
- State in the README that there is NO need to `docker network create`.

### Anti-pattern: `host.docker.internal` for cross-compose calls

Two brittle points at once:

1. The `extra_hosts: host-gateway` Compose directive is forbidden together with `network_mode: service:` ("conflicting options") — you end up with a manual entrypoint script.
2. The OpenClaw base image already ships `172.17.0.1 host.docker.internal` in `/etc/hosts`, so on a custom Compose network (e.g. 172.28.0.0/16) the name resolves to an unreachable address (ENETUNREACH), and an `if ! getent hosts host.docker.internal` guard never fires.

Lesson: do NOT rely on `host.docker.internal` for cross-compose calls. Shared network + DNS hostname is the only stable path.

## `.env` mirror — anchored grep is mandatory

`grep KEY .env | cut -d= -f2` is **forbidden** in multi-service env-mirror commands. Use the anchored form: `grep '^KEY=' .env | cut -d= -f2`.

### Why

`.env` files often have comment lines that mention the key name (usually explaining what the key does). Without the anchor, grep matches the comment line too; if the comment has no `=`, `cut -d= -f2` returns the whole line unchanged → a multi-line value. Bash `$(...)` preserves the embedded newline.

### Example

A comment line mentioning the key made an unanchored `grep KEY .env | cut -d= -f2` return two lines. Written into a second env file, the first line (`KEY=<start of the comment>`) won, the service's API key became comment text, and every call answered 401.

### How to apply

Any time you read a value from one env file into another file or a command chain, start with an anchored grep. In READMEs and docs, always write the `grep '^KEY='` form.

## Restrictive-licence opt-in (public repo)

Non-commercial or otherwise restricted model weights must never land in the default code path of a public repo, not even by accident. The Fish Audio S2 Pro TTS (research licence, non-commercial) shows the pattern:

1. **Separate compose file.** The service lives in `compose/tts-fish.yml`, which runs only when the operator adds it to `COMPOSE_FILE`. Plain `docker compose up -d` or `build` never touches it.
2. **Token gate on the wiring.** Config-init (`config-init/steps/media.mjs`) writes OpenClaw's `tts` block only when `OPENCLAW_TTS_FISH_API_KEY` is set, so the gateway never calls a service that is not running.
3. **Licence stated where the switch is.** The compose file header and the service README name the licence; the build-time HuggingFace download is the acceptance.

### Why

The wrapper code stays MIT (instructions, not redistribution); only the download pulls in the upstream licence. Gated models on HuggingFace work the same way: the operator accepts the licence on the model page.

### How to apply

For any new service with restrictive content (TTS, STT, image gen), follow the same three steps. Never ship licence-restricted content in the default code path.

## openclaw-cli network-namespace dependency

`openclaw-cli` runs with `network_mode: container:<openclaw-gateway-id>` (it shares the gateway's network namespace). So when you force-recreate `openclaw-gateway` (e.g. after a config-init change), the gateway gets a **new container ID**, while `openclaw-cli` still points at the old (now dead) ID → no network is reachable (not the gateway RPC, not vLLM, not SearxNG).

Symptom: `openclaw agent` prints `Gateway agent failed; falling back to embedded`, then `LLM request failed: network connection error`. `docker inspect openclaw-cli --format '{{.HostConfig.NetworkMode}}'` shows the old ID, and `docker ps` lists the CLI with an empty Networks column.

### How to apply

- Whenever you force-recreate `openclaw-gateway`, **immediately** recreate `openclaw-cli` too:

  ```bash
  cd llm/dgx-openclaw-stack && docker compose up -d --force-recreate openclaw-gateway openclaw-cli
  ```

- `depends_on:` does NOT trigger the CLI recreate automatically — this is a network-namespace dependency, not a startup-order one, and Compose doesn't track it.
- Verify after the recreate: `docker inspect openclaw-cli --format '{{.HostConfig.NetworkMode}}'` should match `docker inspect openclaw-gateway --format '{{.Id}}'`.
- The same holds for `openclaw-meshcore-bridge`. Recreate it too (`--profile meshcore`), or the next start fails with "joining network namespace of container: No such container".
- **After a host reboot** the daemon starts containers in no set order. A namespace sharer that comes up before the gateway fails once and is not retried, and `openclaw-cli` has no restart policy.
- **After a gateway restart or recreate** a sharer that was not recreated with it keeps running in the old namespace, with no network, and still shows as "Up".
- `scripts/netns-dependents-up.sh --watch` covers all three cases. It does one pass, then another after every gateway container start (`docker events`). Each pass waits for a healthy gateway, then starts a stopped sharer, restarts one that started before the gateway, and recreates one that points at an old gateway container. `flock` keeps it to one copy; run it from cron so it also comes back after a reboot or a daemon restart:

  ```bash
  S=$PWD/scripts/netns-dependents-up.sh
  ( crontab -l 2>/dev/null | grep -v netns-dependents-up.sh
    echo "@reboot $S --watch >> $HOME/netns-dependents.log 2>&1"
    echo "*/5 * * * * $S --watch >> $HOME/netns-dependents.log 2>&1" ) | crontab -
  ```

  On the reference host, after `docker restart openclaw-gateway` the watcher restarts the bridge and starts the CLI; after a `--force-recreate` of the gateway alone it recreates both. The bridge logs `handshake ok` each time.

## SearxNG bundled-but-default-disabled gotcha

The `searxng` plugin ships **bundled-but-default-disabled** in the OpenClaw image — `plugins.entries.searxng.enabled = true` is required in the config, otherwise the plugin stays in "Status: disabled, Origin: bundled, Error: bundled (disabled by default)" and the `webSearch` tool is dead.

### `keep_only` is not an enable flag

SearxNG's `use_default_settings.engines.keep_only:` discards every engine not in the list, but does NOT flip `disabled: false` on the survivors. Engines shipped with `disabled: true` in upstream defaults (Yahoo, Yep, Mwmbl, Qwant, …) need an explicit per-engine override; an engine listed in `keep_only` alone simply never runs.

```yaml
engines:
  - name: yep
    disabled: false
```

If you add an engine to `keep_only` and it returns no results, check its default `disabled` / `inactive` flag in the upstream `searx/settings.yml`, then query it alone (`&engines=<name>`) and read `unresponsive_engines` — CAPTCHA / 429 / 403 from your IP looks exactly like "no results" to the agent.

## Bridge DNS reachability semantics

Services on the default compose bridge can reach each other by service name (DNS resolution via `hostname:`). They can also reach LAN IPs and public hostnames outbound — Docker bridge networks NAT outbound by default. Use this when wiring remote backends: `LLM_BASE_URL=http://192.168.x.x:8004/v1/` works from inside a container without any extra Docker network config.

What does NOT work: reaching `host.docker.internal` is platform-dependent (works on Docker Desktop, broken on raw Linux).

## `profiles: ["never"]` parking pattern

When a service should exist in the compose file (for documentation, for users with the standard layout) but not start under the current configuration, add `profiles: ["never"]` as a top-level key. `docker compose up` only starts services in the default profile (those with no `profiles:` key).

This is how the remote-backend setup parks `vllm-llm` / `vllm-embedding`. Do NOT comment out the service blocks — that loses their documentation value and makes diffs harder to read.

## `bootstrap.sh` fills placeholders only

`bootstrap.sh`'s `fill KEY VALUE` writes the new value only while the current value is empty or contains `CHANGE_ME`. That makes the script safe to re-run: real values are never overwritten. When adding a new secret, ship it in `.env.example` as a `CHANGE_ME` placeholder and add a `fill` line.
