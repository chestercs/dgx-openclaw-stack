<!--
Thanks for the PR! A short, focused description goes a long way.
-->

## Summary

<!-- 1-3 sentences. What does this change and why? -->

## Type of change

- [ ] Bug fix
- [ ] New feature / enhancement
- [ ] Documentation
- [ ] Hardware-profile tuning (different GPU)
- [ ] Refactor (no behaviour change)

## Verification

- [ ] `node --check patch-config.mjs` passes
- [ ] `docker compose --env-file .env config --services` parses cleanly
- [ ] (For non-trivial changes) brought the stack up on a real host and ran:
  - [ ] `curl -sS http://127.0.0.1:18789/healthz` → `{"ok":true,"status":"live"}`
  - [ ] `openclaw memory status --deep`
  - [ ] `openclaw agent --agent main --message "..."`
- [ ] [`CHANGELOG.md`](../CHANGELOG.md) updated under `[Unreleased]`

## Notes for reviewers

<!-- Anything non-obvious from the diff: trade-offs, follow-ups, related issues. -->
