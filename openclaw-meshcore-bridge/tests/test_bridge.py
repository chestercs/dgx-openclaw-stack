import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

import websockets

import bridge


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_radio_no_reply_reports_protocol_failure(self):
        with patch.object(bridge.MeshCore, 'create_serial', new=AsyncMock(return_value=None)):
            radio = bridge.MeshSide(AsyncMock())
            with self.assertRaisesRegex(ConnectionError, 'no companion protocol response'):
                await radio.connect()

    async def test_handshake_accepts_interleaved_events(self):
        async def serve(ws):
            await ws.send(json.dumps({'type': 'event', 'event': 'connect.challenge'}))
            request = json.loads(await ws.recv())
            self.assertEqual(request['params']['scopes'], ['operator.read', 'operator.write'])
            await ws.send(json.dumps({'type': 'event', 'event': 'tick'}))
            await ws.send(json.dumps({
                'type': 'res', 'id': request['id'], 'ok': True,
                'payload': {'protocol': 4, 'auth': {'deviceToken': 'fixture-device-token'}},
            }))
            await ws.wait_closed()

        async with websockets.serve(serve, '127.0.0.1', 0) as server:
            url = f'ws://127.0.0.1:{server.sockets[0].getsockname()[1]}'
            async with websockets.connect(url) as ws:
                client = bridge.GatewayClient(url, 'fixture-shared-token')
                await client._handshake(ws)
                self.assertEqual(client._device_token, 'fixture-device-token')

    async def test_unrelated_events_do_not_extend_handshake_deadline(self):
        async def serve(ws):
            try:
                await ws.send(json.dumps({'type': 'event', 'event': 'connect.challenge'}))
                await ws.recv()
                for _ in range(100):
                    await ws.send(json.dumps({'type': 'event', 'event': 'tick'}))
                    await asyncio.sleep(0.03)
            except websockets.ConnectionClosed:
                pass

        with patch.object(bridge, 'GW_CONNECT_TIMEOUT_S', 0.12):
            async with websockets.serve(serve, '127.0.0.1', 0) as server:
                url = f'ws://127.0.0.1:{server.sockets[0].getsockname()[1]}'
                async with websockets.connect(url) as ws:
                    client = bridge.GatewayClient(url, 'fixture-shared-token')
                    started = asyncio.get_running_loop().time()
                    with self.assertRaises(asyncio.TimeoutError):
                        await asyncio.wait_for(client._handshake(ws), timeout=1.5)
                    self.assertLess(asyncio.get_running_loop().time() - started, 1.0)


if __name__ == '__main__':
    unittest.main()
