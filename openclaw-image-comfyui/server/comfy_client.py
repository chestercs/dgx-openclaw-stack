"""HTTP client for the operator's existing ComfyUI install.

ComfyUI's API surface used here is undocumented but stable across years
of releases (Comfy-Org/ComfyUI):

  POST /prompt              — submit a workflow JSON, get back a prompt_id
  GET  /history/{prompt_id} — poll until the prompt has `outputs`
  GET  /view                — fetch a saved image by filename + type
  POST /queue               — `{delete: [prompt_id]}` cancels a queued job
  POST /interrupt           — interrupt the currently running prompt

There's also a WebSocket at /ws for real-time progress, but we deliberately
poll instead — a single 30-line polling loop has fewer failure modes than
managing a WS reconnect / out-of-order frame buffer for the bridge's needs.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

import httpx

log = logging.getLogger("comfy-client")


class ComfyUIError(RuntimeError):
    """Generic ComfyUI HTTP error."""


class ComfyUITimeout(TimeoutError):
    """Prompt did not complete within the configured wall-clock budget."""


class ComfyUIRestartedError(ComfyUIError):
    """The ComfyUI process appears to have restarted mid-generation —
    /history/{prompt_id} returns 404 after a confirmed submit. Surfaced
    as a one-line MCP error rather than hanging until the timeout."""


class ComfyClient:
    def __init__(
        self,
        base_url: str,
        *,
        poll_interval_s: float = 0.5,
        poll_backoff_max_s: float = 2.0,
        request_timeout_s: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.poll_interval_s = poll_interval_s
        self.poll_backoff_max_s = poll_backoff_max_s
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(request_timeout_s, connect=10.0),
            follow_redirects=False,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def submit_prompt(self, prompt: dict, client_id: str) -> str:
        """Submit a workflow JSON to ComfyUI's queue. Returns prompt_id."""
        payload = {"prompt": prompt, "client_id": client_id}
        try:
            r = await self._client.post("/prompt", json=payload)
        except httpx.RequestError as e:
            raise ComfyUIError(f"submit failed (network): {type(e).__name__}: {e}") from e
        if r.status_code != 200:
            # ComfyUI returns 400 with a JSON body describing the bad node.
            raise ComfyUIError(f"submit rejected (HTTP {r.status_code}): {r.text[:500]}")
        body = r.json()
        prompt_id = body.get("prompt_id")
        if not prompt_id:
            raise ComfyUIError(f"submit returned no prompt_id: {body!r}")
        return prompt_id

    async def wait_for_completion(self, prompt_id: str, timeout_s: float) -> dict:
        """Poll /history/{prompt_id} until the prompt has outputs or we
        run out the clock. Exponential backoff from poll_interval_s up
        to poll_backoff_max_s."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        delay = self.poll_interval_s
        seen_once = False  # guards the "ComfyUI restarted" detection

        while True:
            now = loop.time()
            if now >= deadline:
                raise ComfyUITimeout(
                    f"prompt {prompt_id} did not finish in {timeout_s:.0f}s"
                )

            try:
                r = await self._client.get(f"/history/{prompt_id}")
            except httpx.RequestError as e:
                # Transient network blip — retry until the deadline.
                log.warning("history poll network error: %s", e)
                await asyncio.sleep(min(delay, deadline - now))
                delay = min(delay * 1.5, self.poll_backoff_max_s)
                continue

            if r.status_code == 404 and seen_once:
                # We saw the prompt at least once and now it's gone —
                # ComfyUI was restarted mid-generation. Don't keep polling.
                raise ComfyUIRestartedError(
                    f"prompt {prompt_id} disappeared from /history "
                    "(ComfyUI likely restarted)"
                )

            if r.status_code == 200:
                body = r.json()
                # /history returns a dict keyed by prompt_id when present.
                entry = body.get(prompt_id)
                if entry:
                    seen_once = True
                    outputs = entry.get("outputs") or {}
                    status_obj = entry.get("status") or {}
                    completed = bool(status_obj.get("completed"))
                    status_str = status_obj.get("status_str")
                    if status_str == "error":
                        # ComfyUI workflow execution failed (e.g. node raised
                        # an exception, OOM, missing model file). Without this
                        # branch the bridge polls forever because `completed`
                        # stays False on error. Surface the failing node and
                        # the underlying exception so the agent can react
                        # instead of hitting the per-call timeout.
                        err_node = err_type = err_exc = "unknown"
                        for m in status_obj.get("messages") or []:
                            if isinstance(m, list) and len(m) >= 2 and m[0] == "execution_error":
                                data = m[1] if isinstance(m[1], dict) else {}
                                err_node = str(data.get("node_id", "?"))
                                err_type = str(data.get("node_type", "?"))
                                err_exc = str(data.get("exception_message", "")).strip() or "no exception message"
                                break
                        raise ComfyUIError(
                            f"prompt {prompt_id} execution failed at node {err_node} "
                            f"({err_type}): {err_exc}"
                        )
                    if completed and outputs:
                        return entry
                    # Entry exists but no outputs yet — keep polling.
            elif r.status_code != 404:
                raise ComfyUIError(
                    f"history poll failed (HTTP {r.status_code}): {r.text[:200]}"
                )

            sleep_for = min(delay, max(0.0, deadline - loop.time()))
            if sleep_for <= 0:
                continue  # one more loop iteration to hit the deadline check
            await asyncio.sleep(sleep_for)
            delay = min(delay * 1.5, self.poll_backoff_max_s)

    async def fetch_image(
        self, filename: str, *, image_type: str = "output", subfolder: str = ""
    ) -> bytes:
        """Fetch a saved image by filename. ComfyUI's /view validates path
        traversal server-side; we pass parameters as query string."""
        try:
            r = await self._client.get(
                "/view",
                params={"filename": filename, "type": image_type, "subfolder": subfolder},
            )
        except httpx.RequestError as e:
            raise ComfyUIError(f"view fetch failed (network): {type(e).__name__}: {e}") from e
        if r.status_code != 200:
            raise ComfyUIError(
                f"view fetch failed for {filename!r} (HTTP {r.status_code})"
            )
        return r.content

    async def cancel(self, prompt_id: str) -> dict:
        """Best-effort cancel: ask the queue to drop the prompt and, if
        it happens to be the running one, interrupt the worker. Either
        call may 404 if the prompt has already finished — that's fine,
        we report what we attempted."""
        attempted: dict[str, Any] = {"prompt_id": prompt_id, "queue_delete": None, "interrupt": None}

        try:
            r = await self._client.post("/queue", json={"delete": [prompt_id]})
            attempted["queue_delete"] = r.status_code
        except httpx.RequestError as e:
            attempted["queue_delete"] = f"network error: {e}"

        try:
            r = await self._client.post("/interrupt")
            attempted["interrupt"] = r.status_code
        except httpx.RequestError as e:
            attempted["interrupt"] = f"network error: {e}"

        return attempted

    async def system_stats(self) -> dict:
        """Diagnostic helper — used by /healthz when the operator wants
        to confirm the bridge can actually reach ComfyUI."""
        r = await self._client.get("/system_stats")
        r.raise_for_status()
        return r.json()


def extract_image_outputs(history_entry: dict) -> list[dict]:
    """Walk a /history entry's outputs and produce a flat list of
    {filename, subfolder, type, node_id} for every saved image. ComfyUI's
    output shape is `{node_id: {"images": [{filename, subfolder, type}]}}`
    keyed by SaveImage node id."""
    found: list[dict] = []
    outputs = history_entry.get("outputs") or {}
    for node_id, node_out in outputs.items():
        for entry in (node_out or {}).get("images") or []:
            if not entry.get("filename"):
                continue
            found.append(
                {
                    "node_id": node_id,
                    "filename": entry["filename"],
                    "subfolder": entry.get("subfolder", ""),
                    "type": entry.get("type", "output"),
                }
            )
    return found
