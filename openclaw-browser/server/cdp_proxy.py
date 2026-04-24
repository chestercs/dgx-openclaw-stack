"""HTTP + WebSocket reverse proxy for Chromium remote-debugging.

One process per profile. Listens on 0.0.0.0:<external_port> and forwards
to 127.0.0.1:<internal_port>, rewriting the HTTP `Host` header to
`localhost:<internal_port>` so Chromium's DNS-rebinding defense
(introduced in Chrome 136+) accepts the request.

Why this exists at all: Chrome 136+ refuses to bind --remote-debugging-port
on anything other than 127.0.0.1, AND validates the HTTP Host header
on every CDP request — non-loopback Host returns
"500 Internal Server Error: Host header is specified and is not an IP
address or localhost." A pure-TCP forwarder (socat) is enough for the
binding part but fails the Host validation. This proxy rewrites Host.

Usage:
    python -m cdp_proxy <external_port> <internal_port>

Trapping SIGTERM cleanly so the supervisor's stop_profile() can reap us
without a SIGKILL window.
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys

import aiohttp
from aiohttp import web

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s cdp_proxy[%(process)d]: %(message)s")
log = logging.getLogger("cdp_proxy")

# CDP HTTP responses are tiny (a few KB at most for /json/list with many
# tabs). 1 MB is generous; bigger would only matter for trace exports
# which CDP delivers over WebSocket, not HTTP.
HTTP_MAX_BODY = 1 << 20


async def proxy_ws(request: web.Request, internal_port: int) -> web.WebSocketResponse:
    """Bidirectional WS forwarder. The client (OpenClaw gateway) connects to
    our external port; we open an upstream WS to Chromium on the internal
    port with a rewritten Host header. Bytes flow both ways concurrently.
    """
    ws_client = web.WebSocketResponse(autoclose=False, autoping=False)
    await ws_client.prepare(request)

    upstream_url = f"ws://127.0.0.1:{internal_port}{request.path_qs}"
    upstream_headers = {"Host": f"localhost:{internal_port}"}

    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=0)) as session:
        try:
            ws_up = await session.ws_connect(
                upstream_url,
                headers=upstream_headers,
                heartbeat=None,
                autoclose=False,
                autoping=False,
                max_msg_size=0,  # unlimited; CDP traces can be very large
            )
        except aiohttp.WSServerHandshakeError as exc:
            log.warning("upstream WS handshake failed: %s (%s)", upstream_url, exc)
            await ws_client.close(code=1011, message=b"upstream handshake failed")
            return ws_client

        async def client_to_up() -> None:
            try:
                async for msg in ws_client:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        await ws_up.send_str(msg.data)
                    elif msg.type == aiohttp.WSMsgType.BINARY:
                        await ws_up.send_bytes(msg.data)
                    elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED):
                        break
            finally:
                if not ws_up.closed:
                    await ws_up.close()

        async def up_to_client() -> None:
            try:
                async for msg in ws_up:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        await ws_client.send_str(msg.data)
                    elif msg.type == aiohttp.WSMsgType.BINARY:
                        await ws_client.send_bytes(msg.data)
                    elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED):
                        break
            finally:
                if not ws_client.closed:
                    await ws_client.close()

        await asyncio.gather(client_to_up(), up_to_client(), return_exceptions=True)

    return ws_client


async def proxy_http(request: web.Request, internal_port: int) -> web.Response:
    """Plain HTTP forwarder for CDP discovery (/json/version, /json/list,
    /json/protocol, etc.). Rewrites Host header before forwarding.
    """
    upstream_url = f"http://127.0.0.1:{internal_port}{request.path_qs}"

    # Build headers: copy client's, then override Host. Strip hop-by-hop
    # headers that aiohttp / proxies traditionally don't forward.
    forward_headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in (
            "host", "connection", "keep-alive", "proxy-authenticate",
            "proxy-authorization", "te", "trailers", "transfer-encoding",
            "upgrade", "content-length",
        )
    }
    forward_headers["Host"] = f"localhost:{internal_port}"

    body = await request.read() if request.body_exists else None

    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            async with session.request(
                request.method,
                upstream_url,
                headers=forward_headers,
                data=body,
                allow_redirects=False,
            ) as upstream:
                response_body = await upstream.read()
                # Strip hop-by-hop response headers as well.
                response_headers = {
                    k: v for k, v in upstream.headers.items()
                    if k.lower() not in (
                        "connection", "keep-alive", "transfer-encoding",
                        "content-encoding", "content-length",
                    )
                }
                return web.Response(
                    status=upstream.status,
                    headers=response_headers,
                    body=response_body,
                )
        except aiohttp.ClientConnectorError as exc:
            log.warning("upstream connect failed: %s (%s)", upstream_url, exc)
            return web.Response(status=502, text=f"upstream unreachable: {exc}")
        except asyncio.TimeoutError:
            log.warning("upstream timeout: %s", upstream_url)
            return web.Response(status=504, text="upstream timeout")


def make_handler(internal_port: int):
    async def handler(request: web.Request) -> web.StreamResponse:
        upgrade = request.headers.get("Upgrade", "").lower()
        if upgrade == "websocket":
            return await proxy_ws(request, internal_port)
        return await proxy_http(request, internal_port)

    return handler


async def main(external_port: int, internal_port: int) -> None:
    app = web.Application(client_max_size=HTTP_MAX_BODY)
    handler = make_handler(internal_port)
    # Match every path/method — we're a transparent proxy for whatever
    # Chromium's CDP HTTP server exposes.
    app.router.add_route("*", "/{tail:.*}", handler)

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=external_port, reuse_address=True)
    await site.start()
    log.info(
        "cdp_proxy listening on 0.0.0.0:%s -> 127.0.0.1:%s (Host rewrite: localhost:%s)",
        external_port, internal_port, internal_port,
    )

    stop = asyncio.Event()

    def _on_signal() -> None:
        log.info("signal received, shutting down")
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _on_signal)
        except NotImplementedError:
            pass

    await stop.wait()
    await runner.cleanup()


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("usage: python -m cdp_proxy <external_port> <internal_port>", file=sys.stderr)
        sys.exit(2)
    asyncio.run(main(int(sys.argv[1]), int(sys.argv[2])))
