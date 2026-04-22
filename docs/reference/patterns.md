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

Explicit user feedback: "I want `compose up` to create the network if it doesn't exist and attach to it if it does — no separate `docker network create` step." The `external: true` pattern doesn't solve that (you must `docker network create` manually first time); `external: false` + `name:` does.

### How to apply

- Use this as the default for any new cross-compose setup.
- Services should reference each other by hostname (`bge-m3:8005`, `gemma4-31b:8004`), NOT via `host.docker.internal` / IPs / `extra_hosts`. Cross-platform stable (Linux daemon, Docker Desktop on Mac / Windows).
- The `name:` field is CRITICAL — without it, Compose creates a `<project>_<networkname>` form and the two projects see different networks.
- If a service also needs another internal network (e.g., a private DB), pass a list: `networks: [shared-net, internal-net]`.
- State in the README that there is NO need to `docker network create`.

### Anti-pattern: `host.docker.internal` for cross-compose calls

Two brittle points at once:

1. The `extra_hosts: host-gateway` Compose directive is forbidden together with `network_mode: service:` ("conflicting options") — you end up with a manual entrypoint script.
2. The OpenClaw base image already ships `172.17.0.1 host.docker.internal` in `/etc/hosts` → on a custom Compose network (e.g. 172.28.0.0/16) that's ENETUNREACH. The entrypoint guard (`if ! getent hosts host.docker.internal`) therefore skipped the override.

Lesson: do NOT rely on `host.docker.internal` for cross-compose calls. Shared network + DNS hostname is the only stable path.

## `.env` mirror — anchored grep is mandatory

`grep KEY .env | cut -d= -f2` is **forbidden** in multi-service env-mirror commands. Use the anchored form: `grep '^KEY=' .env | cut -d= -f2`.

### Why

`.env` files often have comment lines that mention the key name (usually explaining what the key does). Without the anchor, grep matches the comment line too; if the comment has no `=`, `cut -d= -f2` returns the whole line unchanged → a multi-line value. Bash `$(...)` preserves the embedded newline.

### Concrete incident (2026-04-22, `c69a9f2`)

`openclaw-tts-router/.env.example` had a comment mentioning `OPENCLAW_TTS_ROUTER_API_KEY`. `grep OPENCLAW_TTS_ROUTER_API_KEY .env | cut -d= -f2` returned a multi-line value, and the `echo "KEY=$ROUTER" >> ../openclaw/.env` call slipped a `KEY=<start-of-comment>` line followed by a lone-hex line into the gateway env file. Docker Compose's `.env` parser took the first line, so the gateway's `apiKey` became the start of the comment → curl 401.

### How to apply

Any time you read a value from one env file into another file or a command chain, start with an anchored grep. In READMEs and docs, always write the `grep '^KEY='` form.

## CC-BY-NC opt-in triple-gate (public repo)

CC-BY-NC model weights must NEVER land in the default code path of a public repo (not even by accident). Proven triple-gate pattern (`dgx-openclaw-stack/openclaw-tts-f5hun`, 2026-04-22):

1. **Compose profile guard** — `profiles: ["hu"]` on the service block. Plain `docker compose up -d` won't start it, plain `docker compose build` won't build it.
2. **Env-token guard** — the fronting / aggregator service (router) only activates the backend when both the matching token and URL are non-empty.
3. **Bootstrap prompt** — `bootstrap.sh` asks once (license disclaimer + readme pointer); if NO → all opt-in env vars stay empty; if YES → it fills all three in one pass (token rotation + URL default + `COMPOSE_PROFILES=hu` append).

### Why

The wrapper code stays MIT (instruction set, not redistribution) — only the build-time HF download triggers acceptance of the upstream model license. Same pattern as Gemma 4 NVFP4 (gated, license acceptance on HF) — battle-tested and compatible with a public MIT repo.

### How to apply

For any new service with CC-BY-NC or other restrictive content (TTS, STT, image gen, etc.), follow this triad. NEVER ship license-restrictive content in the default code path. The `default_hu` reference voice (LibriVox / public domain) is an exception — it's CC0/PD; only the fine-tune weights are NC.

## openclaw-cli network-namespace dependency

`openclaw-cli` runs with `network_mode: container:<openclaw-gateway-id>` (it shares the gateway's network namespace). So when you force-recreate `openclaw-gateway` (e.g. after a patch-config change), the gateway gets a **new container ID**, while `openclaw-cli` still points at the old (now dead) ID → no network is reachable (not the gateway RPC, not vLLM, not SearxNG).

Symptom: `openclaw agent` prints `Gateway agent failed; falling back to embedded`, then `LLM request failed: network connection error`. `docker inspect openclaw-cli --format '{{.HostConfig.NetworkMode}}'` shows the old ID, and `docker ps` lists the CLI with an empty Networks column.

### How to apply

- Whenever you force-recreate `openclaw-gateway`, **immediately** recreate `openclaw-cli` too:

  ```bash
  cd llm/dgx-openclaw-stack && docker compose up -d --force-recreate openclaw-gateway openclaw-cli
  ```

- `depends_on:` does NOT trigger the CLI recreate automatically — this is a network-namespace dependency, not a startup-order one, and Compose doesn't track it.
- Verify after the recreate: `docker inspect openclaw-cli --format '{{.HostConfig.NetworkMode}}'` should match `docker inspect openclaw-gateway --format '{{.Id}}'`.

## SearxNG bundled-but-default-disabled gotcha

The `searxng` plugin ships **bundled-but-default-disabled** in the OpenClaw image — `plugins.entries.searxng.enabled = true` is required in the config, otherwise the plugin stays in "Status: disabled, Origin: bundled, Error: bundled (disabled by default)" and the `webSearch` tool is dead.

### `keep_only` is not an enable flag

SearxNG's `use_default_settings.engines.keep_only:` discards every engine not in the list, but does NOT flip `disabled: false` on the survivors. Engines shipped with `disabled: true` in upstream defaults (Reddit, Wikibooks, Wikiquote, Wikisource, …) need an explicit per-engine override:

```yaml
engines:
  - name: reddit
    disabled: false
```

If you add an engine to `keep_only` and it returns no results, check its default `disabled` flag in the upstream `searx/settings.yml`.

### Cosmetic categories bug

The SearxNG plugin sends the `categories` config as a Python-list literal in POST form-data (`['general', 'news', 'science']`), which SearxNG validates against and warns about — BUT the search still returns results via the fallback default categories; not fatal. If you want a clean log, remove the `categories` field from patch-config.

## Bridge DNS reachability semantics

Services on the default compose bridge can reach each other by service name (DNS resolution via `hostname:`). They can also reach LAN IPs and public hostnames outbound — Docker bridge networks NAT outbound by default. Use this when wiring remote backends: `OPENAI_BASE_URL=http://192.168.x.x:8004/v1` works from inside a container without any extra Docker network config.

What does NOT work: reaching `host.docker.internal` is platform-dependent (works on Docker Desktop, broken on raw Linux).

## `profiles: ["never"]` parking pattern

When a service should exist in the compose file (for documentation, for users with the standard layout) but not start under the current configuration, add `profiles: ["never"]` as a top-level key. `docker compose up` only starts services in the default profile (those with no `profiles:` key).

This is how the remote-backend setup parks `vllm-llm` / `vllm-embedding`. Do NOT comment out the service blocks — that loses their documentation value and makes diffs harder to read.

## `bootstrap.sh` `upsert_env` is regex-gated

`upsert_env KEY NEWVAL PLACEHOLDER_REGEX` only writes the new value if the current value matches the placeholder regex (e.g. `^CHANGE_ME`). That makes the script safe to re-run — real user values never get overwritten. When adding a new secret, follow the pattern: shipped placeholder in `.env.example` starts with `CHANGE_ME`; bootstrap regex matches that prefix.
