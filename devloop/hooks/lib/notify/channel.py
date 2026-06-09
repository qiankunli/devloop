"""`Notifier` over a Claude Code **channel** — the first concrete delivery for the notify port.

A channel is an MCP server (research preview): Claude Code spawns it over stdio, and a
`notifications/claude/channel` event lands in the session as a `<channel source="..." ...>`
tag — an idle session wakes WITH the content inline. `ChannelNotifier` is the delivery;
`run_channel` is the reusable server harness (handshake + the `claude/channel` capability)
that any producer (forge / deploy / verdict) plugs into.

`mcp` is imported lazily so this module loads without the dependency (the abstraction and the
pure producer logic stay testable on a stdlib-only Python); `mcp` is only needed to actually
run a channel. The custom-method notification is built raw from public `mcp.types` — the SDK
has no typed API for a non-standard notification method.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable

from lib.notify.base import Notification


class ChannelNotifier:
    """Delivers a `Notification` by pushing it into the session as a channel event. Holds the
    server's write stream (handed in by `run_channel`)."""

    def __init__(self, write_stream) -> None:
        self._write = write_stream

    async def deliver(self, notification: Notification) -> None:
        from mcp.shared.message import SessionMessage
        from mcp.types import JSONRPCMessage, JSONRPCNotification

        note = JSONRPCNotification(
            jsonrpc="2.0",
            method="notifications/claude/channel",
            params={"content": notification.content,
                    "meta": {"kind": notification.kind, **notification.meta}},
        )
        await self._write.send(SessionMessage(message=JSONRPCMessage(note)))


async def run_channel(
    name: str, instructions: str, produce: Callable[[ChannelNotifier], Awaitable[None]]
) -> None:
    """Run an MCP channel server for the session's lifetime: handshake + declare the
    `claude/channel` capability, then drive `produce(notifier)` — an async producer that pushes
    `Notification`s whenever its source changes. Never returns until the session closes the
    transport."""
    import anyio
    from mcp.server.lowlevel import Server
    from mcp.server.stdio import stdio_server

    server = Server(name, instructions=instructions)
    init_opts = server.create_initialization_options(
        experimental_capabilities={"claude/channel": {}}
    )
    async with stdio_server() as (read_stream, write_stream):
        notifier = ChannelNotifier(write_stream)
        async with anyio.create_task_group() as tg:
            tg.start_soon(produce, notifier)
            await server.run(read_stream, write_stream, init_opts)
