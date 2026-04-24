"""Per-profile Chromium supervisor + login helper state machine.

One Chromium process per OpenClaw `browser.profiles.<name>` entry. Each
process owns a `--user-data-dir` (cookies, localStorage, IndexedDB persist
across restarts) and binds its own remote-debugging port. Ports are
deterministic: default profile = BROWSER_PORT_BASE (9222), then named
profiles in the order they appear in BROWSER_PROFILE_NAMES (comma-separated).

Login helper: when an operator runs `./bootstrap-browser-login.sh <name>`,
the API endpoint starts Xvfb + x11vnc + websockify + a HEADFUL Chromium on
the same user-data-dir, returns a noVNC URL. The operator drives the auth
flow through their laptop's browser, then POSTs `/finish`; we flush
Chromium cleanly (so cookies persist), tear down the VNC stack, and
re-launch headless on the same user-data-dir.

WHY a custom subprocess wrapper rather than Playwright's
launch_persistent_context: we need (a) the process to outlive the FastAPI
request that started it, (b) deterministic port assignment for OpenClaw's
cdpUrl, and (c) clean SIGTERM / wait semantics for the login-helper finish
flow. Playwright's API is built around in-request lifecycle which doesn't
fit. We still use Playwright to discover the Chromium binary path so we
inherit the upstream-pinned Chromium version from the base image.
"""
from __future__ import annotations

import glob
import logging
import os
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger("browser.supervise")

STORAGE_DIR = Path(os.environ.get("BROWSER_STORAGE_DIR", "/storage"))
DEFAULT_PROFILE_NAME = "default"
PORT_BASE = int(os.environ.get("BROWSER_PORT_BASE", "9222"))
# Internal port that Chromium itself binds on 127.0.0.1 inside the container.
# A `socat` per profile forwards 0.0.0.0:<external_port> → 127.0.0.1:<internal>.
# Required because Chrome >=136 ignores --remote-debugging-address=0.0.0.0
# (https://chromestatus.com/feature/5048140188598272).
INTERNAL_PORT_BASE = int(os.environ.get("BROWSER_INTERNAL_PORT_BASE", "19222"))
MAX_PROFILES = int(os.environ.get("BROWSER_MAX_PROFILES", "20"))
DISPLAY_FOR_HEADFUL = os.environ.get("BROWSER_DISPLAY", ":99")


_chromium_executable: Optional[str] = None


def _find_chromium() -> str:
    """Discover the Chromium binary path inside the Playwright base image.

    The mcr.microsoft.com/playwright/python image installs Chromium at
    /ms-playwright/chromium-<rev>/chrome-linux/chrome at build time. We
    glob the path so we don't pin the revision number — when the base
    image bumps Playwright + Chromium, this still resolves to the new
    binary. Cached after first call.

    We deliberately do NOT call `playwright.sync_api.sync_playwright()`
    here: this function runs from FastAPI's startup hook, which is
    inside the asyncio loop, and Playwright's sync API refuses to start
    in that context (raises "It looks like you are using Playwright Sync
    API inside the asyncio loop"). We only need the binary path; the
    glob delivers it without touching Playwright's process model.
    """
    global _chromium_executable
    if _chromium_executable:
        return _chromium_executable
    candidates = sorted(glob.glob("/ms-playwright/chromium-*/chrome-linux/chrome"))
    if not candidates:
        raise RuntimeError(
            "No Chromium binary found under /ms-playwright/. The Playwright "
            "base image should have installed it during build. Rebuild with "
            "`docker compose --profile browser build --no-cache openclaw-browser` "
            "and watch for `playwright install chromium` failing."
        )
    path = candidates[-1]
    if not Path(path).exists():
        raise RuntimeError(f"Chromium binary at {path!r} is listed by glob but missing on disk")
    _chromium_executable = path
    log.info("chromium binary path: %s", path)
    return path


def parse_profile_names(env_value: str | None) -> list[str]:
    """BROWSER_PROFILE_NAMES is comma-separated in .env (newlines don't play
    well with shell-style env files). Strip whitespace, drop empties."""
    if not env_value:
        return []
    return [n.strip() for n in env_value.split(",") if n.strip()]


@dataclass
class Profile:
    name: str
    port: int                                       # external (published) port — what cdpUrl points at
    internal_port: int                              # 127.0.0.1 port Chromium binds on
    user_data_dir: Path
    process: Optional[subprocess.Popen] = None      # Chromium
    socat_process: Optional[subprocess.Popen] = None  # TCP forwarder external→internal
    started_at: float = 0.0
    headful: bool = False

    def is_running(self) -> bool:
        chromium_alive = self.process is not None and self.process.poll() is None
        socat_alive = self.socat_process is not None and self.socat_process.poll() is None
        return chromium_alive and socat_alive


class Supervisor:
    def __init__(self) -> None:
        STORAGE_DIR.mkdir(parents=True, exist_ok=True)
        self._profiles: dict[str, Profile] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Port allocation. Default = PORT_BASE; named profiles are 1-indexed
    # off PORT_BASE in registration order. We use the env-declared order
    # first (so the patcher's cdpUrl entries line up), then fall back to
    # next-free-port for ad-hoc profiles created via the API.
    # ------------------------------------------------------------------
    def assign_port(self, name: str, env_order: list[str] | None = None) -> int:
        if name == DEFAULT_PROFILE_NAME:
            return PORT_BASE
        env_order = env_order or parse_profile_names(os.environ.get("BROWSER_PROFILE_NAMES"))
        if name in env_order:
            idx = env_order.index(name)
            port = PORT_BASE + 1 + idx
            if port > PORT_BASE + MAX_PROFILES - 1:
                raise ValueError(
                    f"profile '{name}' index {idx} exceeds MAX_PROFILES={MAX_PROFILES}; "
                    "expand the port range in docker-compose.yml and bump BROWSER_MAX_PROFILES."
                )
            return port
        used = {p.port for p in self._profiles.values()}
        for port in range(PORT_BASE + 1, PORT_BASE + MAX_PROFILES):
            if port not in used:
                return port
        raise ValueError(f"no free port within range {PORT_BASE}-{PORT_BASE + MAX_PROFILES - 1}")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def list_profiles(self) -> list[dict]:
        with self._lock:
            return [
                {
                    "name": p.name,
                    "port": p.port,
                    "internal_port": p.internal_port,
                    "user_data_dir": str(p.user_data_dir),
                    "running": p.is_running(),
                    "started_at": p.started_at,
                    "headful": p.headful,
                }
                for p in self._profiles.values()
            ]

    def get(self, name: str) -> Optional[Profile]:
        with self._lock:
            return self._profiles.get(name)

    def _internal_port_for(self, external_port: int) -> int:
        """Map external port (9222+offset) → internal Chromium port (19222+offset).
        Pure offset arithmetic so the mapping is deterministic without state."""
        return INTERNAL_PORT_BASE + (external_port - PORT_BASE)

    def start_profile(self, name: str, *, headful: bool = False, display: str = DISPLAY_FOR_HEADFUL) -> Profile:
        with self._lock:
            existing = self._profiles.get(name)
            if existing and existing.is_running() and existing.headful == headful:
                return existing
            if existing and existing.is_running():
                # Mode change (headless ↔ headful) — restart.
                self._stop_locked(existing)

            user_data_dir = STORAGE_DIR / name
            user_data_dir.mkdir(parents=True, exist_ok=True)

            port = existing.port if existing else self.assign_port(name)
            internal_port = self._internal_port_for(port)
            chromium = _find_chromium()

            args = [
                chromium,
                f"--user-data-dir={user_data_dir}",
                # Chrome >=136 ignores --remote-debugging-address; the binary
                # always listens on 127.0.0.1 only. We launch on an internal
                # port and put a socat in front to expose 0.0.0.0:<port>.
                f"--remote-debugging-port={internal_port}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-default-apps",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                # Chromium's user-namespace sandbox requires CAP_SYS_ADMIN
                # which we don't grant; --no-sandbox is the documented
                # alternative for hardened-container Chromium.
                "--no-sandbox",
                # Avoid the "First Run" balloon and password manager prompts
                # — they wreck the headful login UX and serve no purpose
                # for headless automation.
                "--password-store=basic",
                "--use-mock-keychain",
            ]
            if headful:
                args.append("--start-maximized")
            else:
                # `--headless=new` is the modern (2024+) headless mode that
                # uses the same renderer as the visible Chrome. Older
                # `--headless` is deprecated; never use it on Playwright 1.58+.
                args.append("--headless=new")

            env = os.environ.copy()
            if headful:
                env["DISPLAY"] = display
            env.pop("LANG", None)  # avoid locale-dependent test flakes

            log.info(
                "launching Chromium profile=%s external=%s internal=%s headful=%s user_data_dir=%s",
                name, port, internal_port, headful, user_data_dir,
            )
            proc = subprocess.Popen(
                args,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                # Don't share stdin — Chromium's debug surface is via the port.
                stdin=subprocess.DEVNULL,
            )

            profile = existing or Profile(
                name=name, port=port, internal_port=internal_port, user_data_dir=user_data_dir,
            )
            profile.process = proc
            profile.started_at = time.time()
            profile.headful = headful
            self._profiles[name] = profile

            # Wait for Chromium's debug port to come up before launching socat
            # — socat would otherwise immediately fail TCP connects upstream.
            if not self._wait_for_port(internal_port, timeout=10.0):
                log.error("Chromium profile=%s didn't open internal port %s", name, internal_port)

            # Spawn the TCP forwarder. socat fork + reuseaddr lets multiple
            # parallel attaches share the listener cleanly.
            socat_args = [
                "socat",
                f"TCP-LISTEN:{port},fork,reuseaddr,bind=0.0.0.0",
                f"TCP:127.0.0.1:{internal_port}",
            ]
            log.info("launching socat profile=%s 0.0.0.0:%s -> 127.0.0.1:%s", name, port, internal_port)
            socat_proc = subprocess.Popen(
                socat_args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
            )
            profile.socat_process = socat_proc

            # Best-effort wait for the external port to open. socat usually
            # binds in under 100 ms, but give it a generous 3 s window.
            self._wait_for_port(port, timeout=3.0)
            return profile

    def _wait_for_port(self, port: int, *, timeout: float) -> bool:
        import socket
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    return True
            except OSError:
                time.sleep(0.2)
        log.warning("Chromium debug port %s did not open within %ss", port, timeout)
        return False

    def _stop_locked(self, profile: Profile, *, timeout: float = 10.0) -> bool:
        # Stop Chromium first (so it can flush cookies cleanly), then socat.
        # If we kill socat first, Chromium thinks its CDP client crashed and
        # sometimes lingers writing crash reports.
        proc = profile.process
        if proc and proc.poll() is None:
            log.info("stopping Chromium profile=%s pid=%s", profile.name, proc.pid)
            try:
                proc.send_signal(signal.SIGTERM)
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                log.warning("Chromium profile=%s did not exit on SIGTERM after %ss; SIGKILL", profile.name, timeout)
                proc.kill()
                try:
                    proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    log.error("Chromium profile=%s ignored SIGKILL — process leaked", profile.name)
        profile.process = None

        socat_proc = profile.socat_process
        if socat_proc and socat_proc.poll() is None:
            log.info("stopping socat profile=%s pid=%s", profile.name, socat_proc.pid)
            try:
                socat_proc.terminate()
                socat_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                socat_proc.kill()
        profile.socat_process = None
        return True

    def stop_profile(self, name: str, *, timeout: float = 10.0) -> bool:
        with self._lock:
            profile = self._profiles.get(name)
            if not profile:
                return False
            return self._stop_locked(profile, timeout=timeout)

    def restart_profile(self, name: str, *, headful: bool = False, display: str = DISPLAY_FOR_HEADFUL) -> Profile:
        with self._lock:
            self.stop_profile(name)
            return self.start_profile(name, headful=headful, display=display)

    def delete_profile(self, name: str) -> bool:
        if name == DEFAULT_PROFILE_NAME:
            raise ValueError("default profile cannot be deleted")
        with self._lock:
            self.stop_profile(name)
            profile = self._profiles.pop(name, None)
            if profile and profile.user_data_dir.exists():
                shutil.rmtree(profile.user_data_dir, ignore_errors=True)
            return profile is not None

    def stop_all(self) -> None:
        with self._lock:
            for name in list(self._profiles.keys()):
                self.stop_profile(name)


# ----------------------------------------------------------------------
# Login helper — single-operator state machine. At most one headful
# Chromium with VNC bridge is active at a time. The operator runs
# bootstrap-browser-login.sh, which POSTs to /v1/sessions/<n>/login-helper,
# walks through the noVNC URL, then POSTs /finish.
# ----------------------------------------------------------------------
@dataclass
class _HelperProcs:
    xvfb: Optional[subprocess.Popen] = None
    x11vnc: Optional[subprocess.Popen] = None
    websockify: Optional[subprocess.Popen] = None


class LoginHelper:
    def __init__(self, supervisor: Supervisor) -> None:
        self.supervisor = supervisor
        self._lock = threading.Lock()
        self._active_profile: Optional[str] = None
        self._procs: _HelperProcs = _HelperProcs()
        self._otp: Optional[str] = None

    def is_active(self) -> bool:
        return self._active_profile is not None

    def active_profile(self) -> Optional[str]:
        return self._active_profile

    def start(
        self,
        profile: str,
        otp: str,
        *,
        vnc_port: int = 5901,
        display: str = DISPLAY_FOR_HEADFUL,
    ) -> dict:
        with self._lock:
            if self._active_profile:
                raise RuntimeError(
                    f"login helper already active for profile '{self._active_profile}' — "
                    f"call /finish on that one first"
                )

            log.info("login-helper start: profile=%s vnc_port=%s display=%s", profile, vnc_port, display)

            # 1. Xvfb on the chosen display. -nolisten tcp keeps the X
            #    server unreachable from the network — only x11vnc on
            #    loopback can attach to it.
            self._procs.xvfb = subprocess.Popen(
                ["Xvfb", display, "-screen", "0", "1280x800x24", "-nolisten", "tcp"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            time.sleep(0.5)

            # 2. Build a passwd file for x11vnc — keeps the OTP out of the
            #    process command line (where `ps` would expose it).
            passwd_file = Path(f"/tmp/x11vnc-{profile}.passwd")
            subprocess.run(
                ["x11vnc", "-storepasswd", otp, str(passwd_file)],
                check=True, stdout=subprocess.DEVNULL,
            )
            # 0600 — only the running user reads it.
            os.chmod(passwd_file, 0o600)

            # 3. x11vnc on loopback inside the container. websockify is the
            #    public face — x11vnc itself never accepts a non-loopback
            #    TCP client, so a port-scanner on the bridge can't reach it.
            self._procs.x11vnc = subprocess.Popen(
                [
                    "x11vnc",
                    "-display", display,
                    "-rfbport", "5900",
                    "-localhost",
                    "-rfbauth", str(passwd_file),
                    "-noxdamage", "-noxkb",
                    "-shared",
                    "-forever",
                ],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            time.sleep(0.4)

            # 4. websockify wraps x11vnc's RFB stream in WebSocket and
            #    serves the noVNC HTML/JS bundle from /usr/share/novnc.
            #    Bind 0.0.0.0 inside container; compose's loopback host
            #    bind keeps it off the LAN.
            self._procs.websockify = subprocess.Popen(
                [
                    "websockify",
                    "--web", "/usr/share/novnc",
                    f"0.0.0.0:{vnc_port}",
                    "127.0.0.1:5900",
                ],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            time.sleep(0.3)

            # 5. Stop any headless Chromium for this profile and start it
            #    headful on the Xvfb display. Same user-data-dir, same port
            #    — when the operator finishes and we restart headless,
            #    cookies they accumulated are right there waiting.
            self.supervisor.restart_profile(profile, headful=True, display=display)

            self._active_profile = profile
            self._otp = otp

            return {
                "profile": profile,
                "vnc_port": vnc_port,
                "vnc_url": f"http://127.0.0.1:{vnc_port}/vnc.html?host=127.0.0.1&port={vnc_port}&password={otp}",
                "expires_in_seconds": 1800,
                "next_step": (
                    "Open the URL in your laptop browser, complete the auth flow "
                    "(password + TOTP / SMS — passkeys won't work over noVNC), "
                    "then POST /v1/sessions/{profile}/login-helper/finish."
                ),
            }

    def finish(self) -> dict:
        with self._lock:
            if not self._active_profile:
                raise RuntimeError("no active login helper to finish")
            profile_name = self._active_profile

            # 1. Stop the headful Chromium cleanly so cookies flush to disk.
            self.supervisor.stop_profile(profile_name)

            # 2. Tear down the VNC chain in reverse-launch order.
            for proc, name in [
                (self._procs.websockify, "websockify"),
                (self._procs.x11vnc, "x11vnc"),
                (self._procs.xvfb, "Xvfb"),
            ]:
                if proc and proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        log.warning("login-helper: %s ignored SIGTERM, killing", name)
                        proc.kill()
            self._procs = _HelperProcs()

            # 3. Wipe the OTP passwd file.
            passwd_file = Path(f"/tmp/x11vnc-{profile_name}.passwd")
            if passwd_file.exists():
                passwd_file.unlink()

            # 4. Re-launch Chromium headless on the saved user-data-dir.
            self.supervisor.start_profile(profile_name, headful=False)

            self._active_profile = None
            self._otp = None
            return {"profile": profile_name, "status": "saved"}

    def cancel(self) -> dict:
        """Same as finish() but does NOT relaunch Chromium headless. Used when
        the operator aborts the helper (Ctrl-C in the bootstrap script).
        Cookies that were captured up to the cancel time still persist in
        the user-data-dir (Chromium flushes on SIGTERM)."""
        with self._lock:
            if not self._active_profile:
                return {"status": "noop"}
            profile_name = self._active_profile
            self.supervisor.stop_profile(profile_name)
            for proc in [self._procs.websockify, self._procs.x11vnc, self._procs.xvfb]:
                if proc and proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
            self._procs = _HelperProcs()
            passwd_file = Path(f"/tmp/x11vnc-{profile_name}.passwd")
            if passwd_file.exists():
                passwd_file.unlink()
            self._active_profile = None
            self._otp = None
            return {"profile": profile_name, "status": "cancelled"}
