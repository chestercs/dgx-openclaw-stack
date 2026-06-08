"""In-process Python kernel pool for the sandbox.

Each session_id maps to a long-lived ipykernel child process; state
(variables, imports, dataframes) survives across calls within the same
session. python_session_reset(session_id) tears the kernel down and the
next python_exec call starts a fresh one. Idle kernels are reaped after
KERNEL_IDLE_TTL_S to keep memory bounded — the next call against a
reaped session_id transparently starts a new kernel (state is lost).

We use jupyter_client.MultiKernelManager directly rather than running a
separate Jupyter Kernel Gateway HTTP server. The gateway adds nothing we
need (the only consumer is in-process); skipping it removes a hop per
call and the auth/shutdown coordination of a second process.

Threading model: jupyter_client's KernelManager / KernelClient are
synchronous and thread-safe per-instance, but their iopub poll
(get_iopub_msg) blocks. We wrap blocking calls in asyncio.to_thread()
so the FastAPI event loop stays responsive while a kernel is computing.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional

from jupyter_client.multikernelmanager import MultiKernelManager

log = logging.getLogger("kernel-pool")

# Secrets that the server process (app.py MCP handlers) legitimately needs
# but that user-submitted code in the kernel must NOT be able to read via
# os.environ. The kernel runs arbitrary Python from Discord users; leaking
# the STT bearer token (or the sandbox's own auth token) into that namespace
# would defeat the "dedicated MCP tool holds the token" isolation. We strip
# them from the child kernel's environment at start_kernel() time. The MCP
# tool implementations in app.py read these from the *uvicorn* process env,
# which is untouched. (2026-06-08 — added with the transcribe_audio tool.)
_KERNEL_ENV_DENYLIST = {
    "PYTHON_SANDBOX_API_TOKEN",
    "STT_API_TOKEN",
    "HUGGING_FACE_HUB_TOKEN",
    "HF_TOKEN",
}


def _kernel_env() -> dict[str, str]:
    """Copy the process environment minus the secret denylist, so the
    child ipykernel inherits PATH/PYTHONPATH/etc. but not bearer tokens."""
    return {k: v for k, v in os.environ.items() if k not in _KERNEL_ENV_DENYLIST}


@dataclass
class _Entry:
    kernel_id: str
    last_used: float = field(default_factory=time.time)


class KernelPool:
    """Session-keyed Python kernel manager.

    Methods are async so they cooperate with FastAPI; the underlying
    jupyter_client calls are sync and run on the default thread executor.

    Session locks live in a separate `_locks` dict (not on `_Entry`) so
    two callers racing on the same `session_id` always observe the same
    lock object — even when one of them finds `_sessions[sid]` empty and
    has to start a kernel. Both dicts are popped together on reset/reap
    so neither leaks across long-running deployments.
    """

    def __init__(self, idle_ttl_s: float = 1800.0):
        self._mkm = MultiKernelManager()
        self._mkm.kernel_name = "python3"
        self._sessions: dict[str, _Entry] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()
        self._idle_ttl_s = idle_ttl_s

    async def _ensure_kernel(self, sid: str) -> str:
        """Return a live kernel_id for the given session, starting one if needed."""
        async with self._global_lock:
            entry = self._sessions.get(sid)
            if entry is not None and entry.kernel_id in self._mkm:
                entry.last_used = time.time()
                return entry.kernel_id
            # Stale or missing entry — start fresh. Pass a token-stripped
            # env so user code in the kernel can't read os.environ secrets
            # (the transcribe_audio MCP tool holds the STT token server-side).
            self._sessions.pop(sid, None)
            kernel_id = await asyncio.to_thread(
                lambda: self._mkm.start_kernel(env=_kernel_env())
            )
            self._sessions[sid] = _Entry(kernel_id=kernel_id)
            log.info("started kernel session=%s kernel_id=%s", sid, kernel_id)
            return kernel_id

    async def session_lock(self, sid: str) -> asyncio.Lock:
        """Per-session lock so concurrent python_exec calls on the same
        session serialize (a kernel processes one execute_request at a time).

        Lives in `_locks`, not on `_Entry`, so it survives the brief
        moment when `_ensure_kernel` pops a stale entry — without that
        the lock would be replaced mid-flight and two executes would run
        without serialization.
        """
        async with self._global_lock:
            if sid not in self._locks:
                self._locks[sid] = asyncio.Lock()
            return self._locks[sid]

    async def execute(
        self,
        sid: str,
        code: str,
        timeout_s: float,
        max_output_bytes: int,
    ) -> dict:
        """Run ``code`` in the session's kernel and collect the iopub stream.

        Returns a dict shaped:
          { stdout, stderr, result, plots: [base64 png], duration_ms,
            error: { type, message, traceback } | None }
        """
        lock = await self.session_lock(sid)
        async with lock:
            kernel_id = await self._ensure_kernel(sid)
            km = self._mkm.get_kernel(kernel_id)
            client = km.client()
            client.start_channels()
            try:
                start = time.monotonic()
                msg_id = await asyncio.to_thread(client.execute, code)
                stdout: list[str] = []
                stderr: list[str] = []
                result_text: list[str] = []
                plots: list[str] = []
                error: Optional[dict] = None
                total_bytes = 0
                truncated = False
                deadline = start + timeout_s
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        error = {
                            "type": "TimeoutError",
                            "message": f"execution exceeded {timeout_s:.1f}s",
                            "traceback": "",
                        }
                        # Best-effort interrupt; doesn't kill the kernel,
                        # just tells the running statement to stop.
                        await asyncio.to_thread(km.interrupt_kernel)
                        break
                    try:
                        msg = await asyncio.to_thread(
                            client.get_iopub_msg, timeout=min(remaining, 1.0)
                        )
                    except Exception:
                        # Empty queue / timeout. Loop and re-check the deadline.
                        continue
                    if msg.get("parent_header", {}).get("msg_id") != msg_id:
                        # Stale or unrelated message — ignore.
                        continue
                    mtype = msg.get("msg_type")
                    content = msg.get("content", {}) or {}
                    if mtype == "stream":
                        text = content.get("text", "")
                        target = stdout if content.get("name") == "stdout" else stderr
                        if not truncated:
                            allowed = max_output_bytes - total_bytes
                            if len(text) <= allowed:
                                target.append(text)
                                total_bytes += len(text)
                            else:
                                if allowed > 0:
                                    target.append(text[:allowed])
                                target.append("\n[output truncated]")
                                truncated = True
                    elif mtype in ("execute_result", "display_data"):
                        data = content.get("data", {}) or {}
                        png = data.get("image/png")
                        if png and not truncated:
                            plots.append(png)  # already base64-encoded by the kernel
                        plain = data.get("text/plain")
                        if plain:
                            result_text.append(plain)
                    elif mtype == "error":
                        error = {
                            "type": content.get("ename", "Error"),
                            "message": content.get("evalue", ""),
                            "traceback": "\n".join(content.get("traceback", []) or []),
                        }
                    elif mtype == "status" and content.get("execution_state") == "idle":
                        break
                duration_ms = int((time.monotonic() - start) * 1000)
                return {
                    "stdout": "".join(stdout),
                    "stderr": "".join(stderr),
                    "result": "\n".join(result_text) if result_text else None,
                    "plots": plots,
                    "duration_ms": duration_ms,
                    "truncated": truncated,
                    "error": error,
                }
            finally:
                client.stop_channels()

    async def reset(self, sid: str) -> bool:
        """Shut down the session's kernel; returns True if one existed.

        Pops both _sessions[sid] and _locks[sid] so neither dict leaks
        across long deployments — a session that comes back gets a fresh
        kernel and a fresh lock anyway.
        """
        async with self._global_lock:
            entry = self._sessions.pop(sid, None)
            self._locks.pop(sid, None)
            if entry is None or not entry.kernel_id:
                return False
            kernel_id = entry.kernel_id
        try:
            await asyncio.to_thread(self._mkm.shutdown_kernel, kernel_id, now=True)
            log.info("reset kernel session=%s kernel_id=%s", sid, kernel_id)
            return True
        except Exception as e:
            log.warning("shutdown failed session=%s: %s", sid, e)
            return False

    async def reap_idle(self) -> int:
        """Shut down kernels idle longer than idle_ttl_s. Returns count reaped."""
        now = time.time()
        victims: list[str] = []
        async with self._global_lock:
            for sid, entry in self._sessions.items():
                if entry.kernel_id and now - entry.last_used > self._idle_ttl_s:
                    victims.append(sid)
        n = 0
        for sid in victims:
            if await self.reset(sid):
                n += 1
        if n:
            log.info("reaped %d idle kernel(s)", n)
        return n

    async def shutdown_all(self) -> None:
        async with self._global_lock:
            sessions = list(self._sessions.keys())
            self._sessions.clear()
        for sid in sessions:
            try:
                await self.reset(sid)
            except Exception:
                pass
