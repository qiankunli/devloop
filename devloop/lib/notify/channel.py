"""Channel transport for the notify port — push a `Source`'s notifications into an open session.

A channel is an MCP server (research preview): Claude Code spawns it over stdio, and a
`notifications/claude/channel` event lands in the session as a `<channel source="..." ...>` tag —
an idle session wakes WITH the content inline. `ChannelNotifier` is the delivery; `run_channel`
is the long-lived runner that pumps a `Source` into the session for the session's lifetime
(multi-wake, set-and-forget).

`mcp` is imported lazily so this module (and `ChannelNotifier`) loads/tests without the dependency
— the abstraction and the source logic stay testable on a stdlib-only Python; `mcp` is only needed
to actually run a channel. The custom-method notification is built raw from public `mcp.types` —
the SDK has no typed API for a non-standard notification method.
"""
from __future__ import annotations

from collections.abc import Callable

from lib.notify.base import Notification, Source

POLL_INTERVAL_SEC = 5  # watch the local state-bus files this often (monitor refreshes ~every 90s)


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


async def _pump(source: Source, repos: Callable[[], list[str]], notifier: ChannelNotifier) -> None:
    """Drive `source` over the repo set forever, delivering every notification it fires. Seeds each
    repo's carry from current state (ignore the startup edge); re-resolves the repo set each tick so
    new subprojects join."""
    import anyio

    carries: dict[str, object] = {r: source.seed(r) for r in repos()}
    while True:
        await anyio.sleep(POLL_INTERVAL_SEC)
        for r in repos():
            carry = carries[r] if r in carries else source.seed(r)
            carry, notes = source.step(r, carry)
            for n in notes:
                await notifier.deliver(n)
            carries[r] = carry


async def run_channel(source: Source, repos: Callable[[], list[str]]) -> None:
    """Run the channel MCP server for the session's lifetime: handshake + declare the
    `claude/channel` capability, then pump `source` into the session via a `ChannelNotifier`.
    `repos()` is re-resolved each tick (the workspace's subproject set). Never returns until the
    session closes the transport."""
    import anyio
    from mcp.server.lowlevel import Server
    from mcp.server.stdio import stdio_server

    server = Server(source.name, instructions=source.instructions)
    init_opts = server.create_initialization_options(
        experimental_capabilities={"claude/channel": {}}
    )
    async with stdio_server() as (read_stream, write_stream):
        notifier = ChannelNotifier(write_stream)
        async with anyio.create_task_group() as tg:
            tg.start_soon(_pump, source, repos, notifier)
            await server.run(read_stream, write_stream, init_opts)
