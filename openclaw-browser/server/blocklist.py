"""Domain-suffix blocklist consulted before launching a navigation.

Loads `/config/blocklist.json` (read-only bind mount) and exposes a single
`is_blocked(host)` predicate. The MVP uses this only at the application layer
— the `/v1/actions` endpoint refuses URLs whose hostname matches a blocked
suffix. Network-level enforcement via Chromium request interception is a
Phase 2 enhancement; this module's signature is forward-compatible with that
move.

Blocklist entries are matched as suffixes: 'bankofamerica.com' blocks
'www.bankofamerica.com' and 'login.bankofamerica.com'. The match is
case-insensitive and dot-anchored (so 'bofamerica.com' would NOT match
'bankofamerica.com').
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from urllib.parse import urlparse

log = logging.getLogger("browser.blocklist")

DEFAULT_PATH = "/config/blocklist.json"


class Blocklist:
    def __init__(self, path: str | os.PathLike = DEFAULT_PATH) -> None:
        self.path = Path(path)
        self._suffixes: tuple[str, ...] = ()
        self.reload()

    def reload(self) -> None:
        if not self.path.exists():
            log.warning("blocklist file missing at %s — no policy enforced", self.path)
            self._suffixes = ()
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            log.error("blocklist file at %s is invalid JSON: %s — falling back to empty", self.path, exc)
            self._suffixes = ()
            return
        suffixes = tuple(s.lower().strip().lstrip(".") for s in data.get("block_suffixes", []) if s.strip())
        self._suffixes = suffixes
        log.info("blocklist loaded: %d suffix(es)", len(suffixes))

    def is_blocked(self, url_or_host: str) -> bool:
        if not self._suffixes:
            return False
        host = url_or_host
        if "://" in url_or_host:
            host = urlparse(url_or_host).hostname or ""
        host = host.lower().strip(".")
        if not host:
            return False
        for suffix in self._suffixes:
            if host == suffix or host.endswith("." + suffix):
                return True
        return False

    def reason(self, url_or_host: str) -> str | None:
        if not self.is_blocked(url_or_host):
            return None
        host = url_or_host
        if "://" in url_or_host:
            host = urlparse(url_or_host).hostname or ""
        return f"host '{host}' matches blocked suffix in {self.path.name}"
