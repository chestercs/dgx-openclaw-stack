# Browser automation — design notes

`openclaw-browser` is the self-hosted Playwright Chromium service that
OpenClaw's built-in `browser` tool attaches to over Chrome DevTools
Protocol. This file captures the design choices and the trade-offs they
imply, so a future maintainer can revisit any of them with full context.

## What it is and isn't

It is a thin supervisor that keeps one warm Chromium per onboarded
credential, plus an anonymous default profile. It exposes an HTTP
management API (FastAPI) for session lifecycle and a one-time noVNC bridge
for the manual OAuth onboarding flow. It does not implement any of the
browser-control tools itself — those live in OpenClaw, which speaks raw
CDP to the per-profile Chromium ports.

It is not an MCP server. OpenClaw has no MCP integration as of the latest
stable image, so the integration path is OpenClaw's native `browser` tool
plus a remote-attach Chromium. We do not implement Playwright tools
ourselves; the gateway-side code does.

It is not a stealth scraper. The Chromium is vanilla Playwright with
`navigator.webdriver=true`; user-owned authenticated accounts (GitHub,
Notion, Patreon, MediaWiki) work fine, hostile-CDN sites with active
bot management (Cloudflare Turnstile, DataDome, PerimeterX) will block
on first request. Patchright is documented as a Phase 2 swap if needed.

## Why CDP-attach, not MCP, not a bespoke HTTP tool adapter

Three integration paths were considered.

**MCP** was the original framing in the wishlist. At v0.7.0 design time
(2026-04-25) the OpenClaw config schema had no slot for MCP servers (no
`plugins.entries.mcp.*`, no `tools.mcp.servers[]`). Adding one would
have required either a custom upstream plugin maintained against every
OpenClaw release, or a wrapper that re-implements every browser tool the
agent needs. Both expanded the surface area we own. Rejected.

Native MCP client support landed in OpenClaw shortly after — `mcp.servers.<name>`
with stdio / SSE-HTTP / Streamable-HTTP transports (verified 2026-04-26).
Net new tool wiring (e.g. the v0.8.0+ Python sandbox) defaults to MCP.
The browser stack stays on CDP-attach because the constraint was
port-per-profile routing (issues #4841 / #9723 / #11926) and CDP
Bearer-token semantics — neither problem is solved by switching transport.
If a future structural reason emerges (multi-host browser pool, per-tool
allow-list shape change), MCP is now a viable alternative.

**Bespoke HTTP tool adapter (SearxNG-style)** would mean a FastAPI
service exposing routes like `POST /v1/fetch` that the agent calls via
OpenClaw's plugin tool API. This works for static fetches but
re-implements navigate / click / fill / extract / snapshot — the entire
control surface OpenClaw already gives us for free via its `browser`
tool. The duplication is the cost. Rejected once we found OpenClaw's
native browser tool supports remote attach.

**CDP attach via `browser.profiles.<name>.cdpUrl`** — what we shipped.
OpenClaw's `browser` tool calls `connectOverCDP(cdpUrl)` against any
Chromium-compatible CDP endpoint. We expose Chromium directly on a
loopback-bound port; OpenClaw's gateway does the rest. Total custom
code is the supervisor + login helper + a small markdown extractor —
~600 lines of Python including comments. The control surface (navigate,
click, fill, type, evaluate, snapshot, screenshot, cookies, storage,
viewport, locale, timezone, geolocation) comes from OpenClaw, not us.

## Why port-per-profile, not one CDP port + `?profile=<name>`

OpenClaw's `cdpUrl` field accepts query strings, but the gateway does NOT
forward `?profile=<name>` to Playwright's `connectOverCDP` call (issues
#4841, #9723, #11926 in the OpenClaw repo confirm this). Each profile
must resolve to a distinct cdpUrl that points at a distinct backing
Chromium.

The natural way to satisfy that is one Chromium per profile, each on its
own port. We allocate ports deterministically:

- 9220 — FastAPI management API (no Chromium here).
- 9222 — `default` anonymous throwaway profile.
- 9223–9241 — named profiles in `BROWSER_PROFILE_NAMES` order.

The patcher (step 15) writes `browser.profiles.<name>.cdpUrl =
http://openclaw-browser:<port>?token=<BROWSER_API_TOKEN>` for each
registered profile. The agent calls `browser.navigate(url=...,
profile="github-user1")` and OpenClaw resolves that to the matching
cdpUrl and connects directly to port 9223.

The 20-port range is configurable via `BROWSER_MAX_PROFILES` plus the
matching range publish in `docker-compose.yml`. Few personal use cases
need more than half a dozen profiles.

## Why query-string token, not Authorization header

OpenClaw's `cdpUrl` config field accepts URL-embedded auth only — query
token (`?token=<...>`) or HTTP Basic (`user:pass@host`). It does not
expose a `headers:` field for the underlying `connectOverCDP` call. We
went with query token because Basic auth's user:pass URL form is
deprecated in modern browsers and worse for machine-to-machine cases.

This is a known security weakening relative to a pure Bearer-header
design: query tokens leak into access logs, `ps` output, and
referrer-style headers. The mitigations we ship:

- `BROWSER_BIND` defaults to 127.0.0.1, so only loopback on the host
  reaches the CDP ports.
- Sibling containers reach Chromium via Docker bridge DNS, an isolated
  network the LAN cannot pivot into.
- `rotate-secrets.sh` rotates `BROWSER_API_TOKEN` on the same cadence
  as every other secret; `--all` includes it by default.
- The FastAPI app configures uvicorn to keep `Authorization` headers
  out of access logs (and the management API uses Bearer headers, not
  query tokens, so the rotation invalidates leaked tokens for both
  surfaces).

If you need to expose CDP on the LAN, do not relax `BROWSER_BIND`
without putting a header-auth reverse proxy in front (Caddy / Traefik
with a Basic-or-Bearer rule that strips the query token before
forwarding). Document in your deployment that the CDP port is
network-reachable; an unauthenticated remote-debugging port has been
the root cause of multiple Chromium credential-theft CVEs.

## Always-on noVNC bridge

Since v0.7.0 the VNC stack is part of the container's normal lifecycle:
Xvfb + x11vnc + websockify spin up at app startup, persist for the
container's lifetime, and authenticate via `BROWSER_VNC_PASSWORD` (set
by `bootstrap.sh`, rotated by `rotate-secrets.sh --all`). Anyone with
the password can attach the noVNC URL any time the container is up:

```bash
curl -fsS -H "Authorization: Bearer $BROWSER_API_TOKEN" \
  http://127.0.0.1:9220/v1/vnc | jq -r .vnc_url
```

Outside an active login-helper session the screen is blank — no headful
Chromium is attached to the bridge's Xvfb display. To peek at a
profile's view, push it into headful mode (next section).

Earlier revisions (≤ v0.6.x) span the VNC chain up only for the
duration of a login-helper session, with a freshly-generated OTP each
time. The OTP-per-session UX was awkward (copy a fresh password every
onboarding round, no peeking-at-the-agent without spinning the helper)
and the persistent password matches how `BROWSER_API_TOKEN` already
works.

### Effective entropy

The legacy VNC RFB Type-2 handshake truncates the password to 8 chars
on the wire, so anything beyond ~48 bits of effective entropy is
theatre. Real defense-in-depth is the loopback bind on
`BROWSER_VNC_BIND` (default `127.0.0.1`) plus the LAN being trusted.
**Don't expose the noVNC port to the public internet.** If you need
remote access, SSH-tunnel the loopback port — the script prints the
autossh recipe automatically when `$SSH_CONNECTION` is set.

## 1x OAuth onboarding flow

The operator wants one manual login per credential, no per-call
re-auth. The flow ships as `bootstrap-browser-login.sh <profile-name>`:

1. Operator runs the script. It POSTs `/v1/sessions/<n>/login-helper`
   to the FastAPI app inside the container — empty body, the bridge
   password lives in `BROWSER_VNC_PASSWORD` and the API picks it up
   from the environment.
2. The service stops the headless Chromium for that profile and
   re-launches it HEADFUL on the bridge's Xvfb display (`:99`), same
   `--user-data-dir=/storage/<n>/` the headless Chromium uses. The
   VNC infrastructure was already running; no per-session spin-up.
3. The script prints the noVNC URL
   `http://127.0.0.1:5901/vnc.html?...&password=<persistent>` (server-
   rendered from the API response, so the host/port/password match the
   container's actual config). If the operator is SSH'd in
   (`$SSH_CONNECTION` set), the script also prints an autossh tunnel
   recipe so the operator's laptop browser can reach the loopback port.
4. The operator opens the URL on their laptop, drives the auth flow:
   password + TOTP / SMS OTP / magic link. Closes the tab.
5. Operator hits Enter in the terminal. The script POSTs `/finish`,
   which closes Chromium cleanly (so cookies flush) and re-launches
   Chromium headless on the same `--user-data-dir`. The VNC bridge
   stays running — only the headful Chromium is replaced.
6. The script appends the profile name to `BROWSER_PROFILE_NAMES` in
   `.env` and runs `docker compose run --rm openclaw-config-init` so
   patcher step 15 writes the new `browser.profiles.<n>.cdpUrl` entry
   (and step 16 idempotently appends the soft-policy block to the
   workspace `AGENTS.md`).

After this, the agent can call `browser.navigate(url=...,
profile="<n>")` and the warm Chromium serves authenticated content —
no further manual steps until the upstream session expires.

### What works over noVNC, what doesn't

| Auth method | Works? | Notes |
|---|---|---|
| Password + TOTP | Yes | Primary recommendation |
| Password + SMS OTP | Yes | Operator types the code received on their phone |
| Magic link | Yes | Operator clicks the link; opens in the same Chromium |
| Platform passkey (Apple Keychain, Windows Hello, Google Password Manager) | No | W3C WebAuthn spec is origin-bound; the platform authenticator on the operator's laptop has no path to the remote Chromium's origin |
| Hardware passkey (USB YubiKey) | No | The container does not pass through the laptop's USB bus |
| Chrome sync autofill | Possible but risky | Requires signing a throwaway Google account into the remote Chromium first; convenience-vs-secret trade-off |

Services that require passkey-only auth (some Google Workspace SSO,
some enterprise SAML setups) **cannot** be onboarded via this flow. Use
the service's API key, PAT, or service-account flow instead, fed
through OpenClaw's existing credential channels.

## Session expiry — what to expect

The Chromium user-data-dir persists across container restarts, so the
session lifetime is whatever the upstream service's cookie policy is.
Verified 2026 numbers, useful for setting expectations:

| Service | Default cookie / session lifetime | 2FA re-auth |
|---|---|---|
| GitHub | 14 days inactive | 28 days from last 2FA event (if 2FA enabled) |
| Notion (free / pro) | ~30 days | not specified |
| Notion Enterprise | up to 180 days (admin-configurable) | admin-configurable |
| Google consumer | variable (14d default, "Stay signed in" extends) | varies |
| Google Workspace | admin-controlled | admin-controlled |
| MediaWiki | 30 days (default `$wgCookieExpiration`) | n/a |
| Patreon | ~30 days (community-reported, undocumented) | n/a |
| Slack | admin-configurable | n/a |
| GitLab | admin-configurable, "Remember me" can extend indefinitely | n/a |
| Discord | 7 days (OAuth tokens), web sessions undocumented | aggressive sign-out on IP change |

When a session expires, the gateway's `browser` tool surfaces a
`session_expired`-style error. The agent or operator re-runs
`./bootstrap-browser-login.sh <profile-name>` — it's idempotent on
existing profile names and refreshes the cookies in place.

The most operator-facing surprise is **GitHub's 28-day 2FA window**: a
session that is otherwise active will still re-prompt for 2FA on day
28 from the last 2FA event. For agents that you rely on for
time-critical work, plan for the re-onboard cadence accordingly, or
use a GitHub PAT (stored in `.env` as a separate variable) and bypass
the browser entirely for GitHub work.

## Markdown extraction lives on our side, not OpenClaw's

OpenClaw's `browser.snapshot` returns an accessibility tree with stable
ref IDs the agent can drive clicks against — perfect for tool-use, not
human-readable. Studio agents that ingest research notes want clean
markdown.

The pipeline is:

1. Agent calls `browser.evaluate(js="document.documentElement.outerHTML",
   profile="github-user1")` to grab the rendered HTML.
2. Agent POSTs the HTML to `POST /v1/extract` on our FastAPI app.
3. We run trafilatura (F1 ≈ 0.96 on Bohemian Rhapsody / open-source
   evaluation sets, multilingual including Hungarian) with
   readability-lxml as a deterministic fallback for pages where
   trafilatura's score is low.
4. The agent gets back `{markdown, title, url, word_count, extractor}`.

Why we keep this on our side and not in OpenClaw: it's a
post-processing concern, not a browser-control concern. OpenClaw's
plugin surface for content extraction is `tools.web.fetch.provider`,
and that field accepts only `firecrawl` or built-in Readability — no
custom extractor injection. Our `/v1/extract` endpoint costs ~50 lines
of code and gives the agent an explicit "extract this HTML to
markdown" call without round-tripping through Firecrawl.

A self-hosted Firecrawl sidecar **is** viable as a Phase 2 optimization
for static fetches that don't need Chromium at all (issue #22256
confirmed `FIRECRAWL_BASE_URL` is overridable). For now, the few-line
trafilatura wrapper is enough.

## Hardening posture

The compose service block ships with:

- `cap_drop: [ALL]` plus a minimum `cap_add: [CHOWN, SETGID, SETUID,
  DAC_OVERRIDE]` — the four caps Chromium needs to spawn its per-tab
  sandbox processes.
- `security_opt: [no-new-privileges:true]`.
- `tmpfs /tmp:size=512m` so a runaway download cannot fill the host's
  tmpfs ceiling.
- `shm_size: 1gb` — Chromium OOMs on smaller `/dev/shm` under any
  moderate workload.
- A `seccomp` profile placeholder at `openclaw-browser/config/seccomp.json`.
  Pull Playwright's upstream profile in for production deployments —
  Docker's default seccomp blocks Chromium-required syscalls
  (`user_faultfd`) and can SIGKILL on certain memory patterns.

Phase 2 hardening options documented but not shipped:

- `userns_mode: dockremap` to map the container's root onto an
  unprivileged host UID. Works on aarch64 in 2026 but adds operator
  overhead.
- Network isolation via a dedicated Docker network without bridge
  access to other services. Useful if the threat model includes a
  malicious page exfiltrating data to internal services; for the
  stated personal-research-assistant use case, the bridge is fine.
- OAuth2 Proxy sidecar to convert the static `?token=` to short-lived
  JWTs. Needed only if the CDP ports are exposed beyond loopback.

## Resource footprint (GB10 reference numbers)

| State | Per Chromium (idle) | Per active scrape (moderate SPA) |
|---|---|---|
| Chromium process | 50–150 MB | — |
| Chromium + 1 blank page | 150–250 MB | — |
| Chromium + 1 GitHub-style page | — | 300–500 MB |
| Chromium + 1 heavy SPA (Figma, Google Docs) | — | 600 MB – 1.2 GB |

Default `BROWSER_MAX_PROFILES=20` × ~250 MB ≈ ~5 GB at idle worst
case. Realistic operator workload (default + 3–4 named profiles) is
~1.2 GB. GB10 free unified memory budget is ~96 GB after the rest of
the stack — non-issue.

CPU is the bottleneck during page load: Chromium uses ~1 core per
active tab for ~3–5 seconds during DOM construction + rendering.
Comfortable parallelism on a 20-core ARM is 3–5 concurrent scrapes;
beyond that, the per-host token bucket queues requests.

## Bot detection boundary

The Chromium ships vanilla — no `playwright-stealth`, `rebrowser-
playwright`, or Patchright. Verified targets:

- **Works**: user-owned authenticated accounts on GitHub, Notion,
  Patreon, MediaWiki, GitLab, Linear (web), Slack, Discord. Anything
  that trusts the logged-in user.
- **Fails**: Cloudflare Turnstile, DataDome, PerimeterX (HUMAN),
  Akamai Bot Manager. Common on news sites, e-commerce, real estate,
  travel booking. Detection is near-instant; vanilla Playwright has no
  defense.

If the use case grows into hostile public scraping, the cheapest swap
is Patchright (Apache 2.0, v1.59.1 in April 2026, binary-level
patches). Pin it in `requirements.txt` instead of `playwright`,
update the import in `supervise.py`, rebuild. Document in
`docs/CUSTOMIZATION.md` when this lands.

## Coaching the model on `browser.act` parameter shapes

`browser.act` routes to different action handlers by `kind`, and each
handler has its own parameter shape — flat `{ref}` for `click`, flat
`{text}` for `type`, but nested `{fields: [{ref, type, value}]}` for
`fill`. Smaller open models (Gemma 4 in particular) routinely call
`fill` with the flat `{element, text}` they used for `click`, get back
`"fill requires fields"` from the normalizer
(`extensions/browser/src/browser/routes/agent.act.normalize.ts:217`),
retry the same broken shape, fill the context with normalizer errors,
and eventually doom-loop into an apology and a "give up" suggestion.

Patcher step 17 (added in v0.7.1) writes a small cheatsheet block into
the workspace `AGENTS.md` showing the right shape for `fill` / `click`
/ `type` next to a labelled wrong shape, plus a one-line recovery hint
("if you see `fill requires fields`, re-emit with `fields: [...]`"). It
sits next to the browser-profile policy block from step 16, in the file
every agent session reads at startup. Idempotent block markers so
re-runs don't duplicate it; cap on length is intentional (a full schema
dump would push out other context every session).

If you swap to a larger / better-tuned model and want to drop the
cheatsheet, comment out step 17 in `patch-config.mjs` — the existing
block in `AGENTS.md` stays, but won't be re-added on fresh installs.

## Known limitations and Phase 2 candidates

- `?profile=` query routing (currently uses port-per-profile) — would
  consolidate to a single port if OpenClaw ever forwards the query
  param. Track upstream issue resolution.
- Stealth — Patchright swap, conditional on need.
- Static-fetch fast path — self-hosted Firecrawl sidecar to bypass
  Chromium for plain HTML. ~50 ms instead of ~3–5 s per fetch.
- Short-lived JWTs on CDP — OAuth2 Proxy sidecar, conditional on need
  to expose CDP beyond loopback.
- Scheduled per-profile Chromium restarts — cron inside container to
  cap RAM creep on long-running profiles. Cookies survive restart, so
  no operator-facing break.
- Network isolation via dedicated Docker network — defends against a
  malicious page pivoting onto bridge-internal services.
