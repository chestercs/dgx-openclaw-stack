# Reusable Docker / dev pattern-ek

> **Public knowledge** — általános, megosztható tanulságok.

## Cross-compose service discovery — `name:` + auto-attach

Ha több külön Docker Compose stack között service-discovery kell (cross-compose DNS), a stabil pattern:

```yaml
networks:
  shared-net:
    name: shared-net      # explicit network name (NEM projekt-prefixelt!)
    driver: bridge
    # NINCS `external: true` — auto-create
```

Mindkét/összes compose ugyanezt a blokkot tartalmazza. Az ELSŐ `compose up` (bármelyik) létrehozza a network-öt, a TÖBBI látja és csak attach-ol. `down`-nál csak az "owner" project (ami létrehozta) törli, label-tracking alapján — amíg bármelyik fent van, a network is.

### Why

User explicit feedback: "úgy volna jó hogy ne kelljen explicit a network create csak a compose up, ami ha nem létezik a network automatikusan megkreálja, de ha létezik akkor meg csatlakozik rá". Az `external: true` minta ezt nem oldja meg (manual `docker network create` kell first-time-on); az `external: false` + `name:` viszont igen.

### How to apply

- Új cross-compose setup-nál használd ezt a mintát az alapból
- A service-eknek hivatkozz a hostname-en (`bge-m3:8005`, `gemma4-31b:8004`), NE `host.docker.internal`-on / IP-n / `extra_hosts`-on. Cross-platform stabil (Linux daemon, Docker Desktop Mac/Win)
- A `name:` mező KRITIKUS — nélküle a Compose `<project>_<networkname>` formában csinálná, és a két project nem ugyanazt a network-öt látná
- Ha a service-nek MÁSIK belső network-re is kell (pl. private DB), `networks: [shared-net, internal-net]` listát adj
- README-be írd bele hogy NINCS szükség `docker network create`-re

### Anti-pattern: `host.docker.internal` cross-compose call

Két törékeny pont egyszerre:
1. `extra_hosts: host-gateway` Compose direktíva → `network_mode: service:` mellett tiltott ("conflicting options"), manuális entrypoint script kell
2. Az openclaw base image alapból tartalmaz `172.17.0.1 host.docker.internal` entry-t a /etc/hosts-ban → custom Compose network-on (172.28.0.0/16) ENETUNREACH. Az entrypoint guard (`if ! getent hosts host.docker.internal`) emiatt skippelte az override-ot

Tanulság: cross-compose hívásnál **ne támaszkodj** `host.docker.internal`-ra. Shared network + DNS hostname az egyetlen stabil út.

## .env mirror — anchored grep kötelező

`grep KEY .env | cut -d= -f2` **tilos** multi-service env-mirror parancsokban. Használj anchored formát: `grep '^KEY=' .env | cut -d= -f2`.

### Why

.env fájlok comment sorai gyakran emlegetik a kulcs nevét (pl. magyarázó megjegyzés, hogy mire való). Anchor nélkül a grep a comment sort is matchelheti; ha a commentben nincs `=`, a `cut -d= -f2` az egész sort változatlanul visszaadja → multi-line érték. A bash `$(...)` megőrzi a középső newline-t.

### Konkrét incidens (2026-04-22, c69a9f2)

`openclaw-tts-router/.env.example` commentje tartalmazta a `OPENCLAW_TTS_ROUTER_API_KEY` nevet. `grep OPENCLAW_TTS_ROUTER_API_KEY .env | cut -d= -f2` multi-line értéket adott vissza, és az `echo "KEY=$ROUTER" >> ../openclaw/.env` becsúsztatott a gateway env-fájlba egy `KEY=<comment-eleje>` sort, utána egy lone-hex sort. Docker compose `.env` parser az első sort vette, a gateway apiKey-je a comment eleje lett → curl 401.

### How to apply

Bármikor amikor env-ből értéket olvasol ki másik fájlba vagy command-line changban, kezdj anchored grep-pel. README-kben és docs-ban is mindig `grep '^KEY='` formát írj.

## CC-BY-NC opt-in triple-gate (publikus repo)

A CC-BY-NC model weights NEM kerülhetnek publikus repo default code path-ba (még véletlenül se). A bevált triple-gate pattern (dgx-openclaw-stack/openclaw-tts-f5hun, 2026-04-22):

1. **Compose profile guard** — `profiles: ["hu"]` a service block-on. Plain `docker compose up -d` nem indítja, plain `docker compose build` nem build-eli
2. **Env-token guard** — a fronting/aggregator service (router) csak akkor activate-eli a backend-et, ha a hozzá tartozó token + URL mindkettő non-empty
3. **Bootstrap prompt** — `bootstrap.sh` egyszer megkérdezi (license disclaimer + readme pointer); ha NO → összes opt-in env üresen marad; ha YES → mind a hármat kitölti egy menetben (token rotation + URL default + `COMPOSE_PROFILES=hu` append)

### Why

A wrapper code MIT marad (instruction set, nem redistribution), csak a build-time HF download triggereli az upstream model license elfogadását. Ugyanaz a pattern mint a Gemma 4 NVFP4 (gated, license accept HF-en) — bevált, kompatibilis a publikus MIT repo-val.

### How to apply

Bármelyik új CC-BY-NC vagy egyéb-restrictive content-ű service-nél (TTS, STT, image gen, stb.) — ezt a triadot kell követni. NE shippelj license-restrictive default code path-ba semmit. A `default_hu` reference voice (LibriVox/PD) az kivétel: az CC0/PD, csak a fine-tune weights NC.

## openclaw-cli network namespace dependency

Az `openclaw-cli` container `network_mode: container:<openclaw-gateway-id>`-vel fut (megosztja a gateway network namespace-ét). Ezért amikor az `openclaw-gateway`-t force-recreate-eled (pl. patch-config változás után), a gateway **új container-ID-t** kap, az openclaw-cli viszont a régi (most már halott) ID-re mutat → semmilyen network nem érhető el (sem gateway-RPC, sem vLLM, sem SearxNG).

Tünet: `openclaw agent` ezt dobja: `Gateway agent failed; falling back to embedded`, majd `LLM request failed: network connection error`. `docker inspect openclaw-cli --format '{{.HostConfig.NetworkMode}}'` régi ID-re mutat, `docker ps` üres Networks oszloppal mutatja.

### How to apply

- Bármikor amikor force-recreate-elsz `openclaw-gateway`-t, **azonnal** recreate-eld az `openclaw-cli`-t is:
  ```bash
  cd llm/dgx-openclaw-stack && docker compose up -d --force-recreate openclaw-gateway openclaw-cli
  ```
- A `depends_on:` nem trigger-eli automatikusan a CLI recreate-jét, mert ez nem startup-order, hanem network-namespace dependency. Compose nem tracking-eli
- Verify a recreate után: `docker inspect openclaw-cli --format '{{.HostConfig.NetworkMode}}'` ID-jének egyeznie kell a `docker inspect openclaw-gateway --format '{{.Id}}'`-vel

## SearxNG bundled-but-default-disabled gotcha

A `searxng` plugin az OpenClaw image-ben **bundled-but-default-disabled** — a `plugins.entries.searxng.enabled = true` explicit kell a configban, különben a plugin "Status: disabled, Origin: bundled, Error: bundled (disabled by default)" állapotban marad és a webSearch tool nem él.

### keep_only nem enable flag

A SearxNG `use_default_settings.engines.keep_only:` discards every engine not in the list, but **does not flip `disabled: false` on the survivors**. Engines shipped with `disabled: true` in upstream defaults (Reddit, Wikibooks, Wikiquote, Wikisource, …) need an explicit per-engine override:

```yaml
engines:
  - name: reddit
    disabled: false
```

Ha enable-elsz egy engine-t a `keep_only` listán de nem ad vissza eredményt, ellenőrizd a default `disabled` flag-et az upstream `searx/settings.yml`-ben.

### Cosmetic categories bug

A SearxNG plugin a `categories` configot Python-list literal-ként küldi POST form-data-ban (`['general', 'news', 'science']`), SearxNG validation error a logban — DE a search visszaad eredményt fallback default kategóriákkal, nem fatal. Ha tisztább log kell, a `categories` mező eltávolítható a patch-config-ból.

## Bridge DNS reachability semantics

Services on the default compose bridge can reach each other by service name (DNS resolution by `hostname:`). They can also reach LAN IPs and public hostnames outbound — Docker bridge networks NAT outbound by default. Use this when wiring remote backends: `OPENAI_BASE_URL=http://192.168.x.x:8004/v1` works from inside a container without any extra Docker network config.

What does *not* work: reaching `host.docker.internal` is platform-dependent (works on Docker Desktop, broken on raw Linux).

## `profiles: ["never"]` parking pattern

Amikor egy service-nek létezni kell a compose file-ban (dokumentáció, standard layout-ot használók), de nem kéne start-olnia a current configban, adj hozzá `profiles: ["never"]` top-level kulcsot. `docker compose up` csak default profile service-eket indít (ahol nincs `profiles:` kulcs).

Így működik a remote-backend setup `vllm-llm` / `vllm-embedding` parking is. **NE comment-eld ki a service block-okat** — az elveszti a documentation value-t és a diff-eket nehézzé teszi.

## bootstrap.sh `upsert_env` regex-gated

`upsert_env KEY NEWVAL PLACEHOLDER_REGEX` only writes the new value if the current value matches the placeholder regex (e.g. `^CHANGE_ME`). Ezzel safe re-running — real user values never get overwritten. Új secret hozzáadásánál a pattern: shipped placeholder in `.env.example` starts with `CHANGE_ME`, bootstrap regex matches that prefix.
