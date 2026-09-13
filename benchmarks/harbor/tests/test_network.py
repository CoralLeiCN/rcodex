from __future__ import annotations

import asyncio
from urllib.parse import urlsplit

import pytest

from benchmarks.harbor.network import forward_http_provider


@pytest.mark.asyncio
async def test_forwarder_streams_both_directions_and_closes() -> None:
    continue_response = asyncio.Event()
    request_received: list[bytes] = []

    async def upstream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request_received.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(b"first")
        await writer.drain()
        await continue_response.wait()
        writer.write(b"second")
        await writer.drain()
        writer.close()

    async with await asyncio.start_server(upstream, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        async with forward_http_provider(f"http://127.0.0.1:{port}/v1", "127.0.0.1") as url:
            target = urlsplit(url)
            assert target.path == "/v1"
            reader, writer = await asyncio.open_connection(target.hostname, target.port)
            request = b"POST /v1/responses HTTP/1.1\r\nAuthorization: Bearer local\r\n\r\n"
            writer.write(request)
            await writer.drain()
            assert await asyncio.wait_for(reader.readexactly(5), 2) == b"first"
            continue_response.set()
            assert await asyncio.wait_for(reader.readexactly(6), 2) == b"second"
            assert request_received == [request]
            # Leave the client open to exercise cleanup of active connections.
        assert await asyncio.wait_for(reader.read(), 2) == b""
        writer.close()
        await writer.wait_closed()
        with pytest.raises(OSError):
            await asyncio.open_connection(target.hostname, target.port)


@pytest.mark.asyncio
async def test_forwarder_rejects_tls_hostname_rewriting() -> None:
    with pytest.raises(ValueError, match="http://"):
        async with forward_http_provider("https://example.test/v1", "host.docker.internal"):
            pytest.fail("HTTPS forwarding must fail before binding")
