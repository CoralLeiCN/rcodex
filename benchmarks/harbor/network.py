"""Benchmark-only TCP forwarding for Docker Desktop to reach a host-accessible API."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import urlsplit, urlunsplit


@asynccontextmanager
async def forward_http_provider(base_url: str, gateway_host: str) -> AsyncIterator[str]:
    """Forward streaming HTTP bytes to one fixed upstream through a loopback socket.

    Docker Desktop exposes host loopback listeners through host.docker.internal.
    No requests, headers, or credentials are logged or buffered as whole responses.
    HTTPS is excluded because rewriting the hostname would change TLS validation.
    """
    upstream = urlsplit(base_url)
    if upstream.scheme != "http" or not upstream.hostname:
        raise ValueError("The Docker Desktop provider forwarder requires an http:// base URL")
    upstream_host, upstream_port = upstream.hostname, upstream.port or 80
    connections: set[asyncio.Task[None]] = set()

    async def pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while data := await reader.read(65_536):
            writer.write(data)
            await writer.drain()
        if writer.can_write_eof():
            writer.write_eof()

    async def connect(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        connections.add(task)
        remote_writer: asyncio.StreamWriter | None = None
        try:
            remote_reader, remote_writer = await asyncio.wait_for(
                asyncio.open_connection(upstream_host, upstream_port), timeout=10
            )
            assert remote_writer is not None
            async with asyncio.TaskGroup() as group:
                group.create_task(pump(reader, remote_writer))
                group.create_task(pump(remote_reader, writer))
        except* (OSError, TimeoutError):
            # Closing the socket lets Codex surface/retry a normal connection error.
            pass
        finally:
            writer.close()
            if remote_writer is not None:
                remote_writer.close()
            connections.discard(task)

    server = await asyncio.start_server(connect, host="127.0.0.1", port=0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield urlunsplit(upstream._replace(netloc=f"{gateway_host}:{port}"))
    finally:
        server.close()
        pending = list(connections)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await server.wait_closed()
