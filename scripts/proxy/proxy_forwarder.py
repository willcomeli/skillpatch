#!/usr/bin/env python3
"""Expose a loopback-only HTTP proxy to Docker bridge containers."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal


BUFFER_SIZE = 64 * 1024


async def copy_stream(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    try:
        while data := await reader.read(BUFFER_SIZE):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        with contextlib.suppress(Exception):
            writer.write_eof()


async def handle_client(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    target_host: str,
    target_port: int,
) -> None:
    peer = client_writer.get_extra_info("peername")
    try:
        target_reader, target_writer = await asyncio.open_connection(
            target_host,
            target_port,
        )
    except OSError as error:
        print(f"connection from {peer} rejected: target unavailable: {error}")
        client_writer.close()
        await client_writer.wait_closed()
        return

    print(f"connection opened: {peer}")
    upstream = asyncio.create_task(copy_stream(client_reader, target_writer))
    downstream = asyncio.create_task(copy_stream(target_reader, client_writer))
    try:
        done, pending = await asyncio.wait(
            {upstream, downstream},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*done, *pending, return_exceptions=True)
    finally:
        target_writer.close()
        client_writer.close()
        await asyncio.gather(
            target_writer.wait_closed(),
            client_writer.wait_closed(),
            return_exceptions=True,
        )
        print(f"connection closed: {peer}")


async def run(args: argparse.Namespace) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    server = await asyncio.start_server(
        lambda reader, writer: handle_client(
            reader,
            writer,
            args.target_host,
            args.target_port,
        ),
        args.listen_host,
        args.listen_port,
    )

    addresses = ", ".join(str(sock.getsockname()) for sock in server.sockets or [])
    print(
        f"proxy forwarder listening on {addresses}; "
        f"forwarding to {args.target_host}:{args.target_port}",
        flush=True,
    )

    async with server:
        await stop.wait()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Forward a Docker-accessible TCP port to a loopback proxy.",
    )
    parser.add_argument("--listen-host", default="172.17.0.1")
    parser.add_argument("--listen-port", type=int, default=17899)
    parser.add_argument("--target-host", default="127.0.0.1")
    parser.add_argument("--target-port", type=int, default=10808)
    return parser.parse_args()


if __name__ == "__main__":
    try:
        asyncio.run(run(parse_args()))
    except KeyboardInterrupt:
        pass
