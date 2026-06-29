"""Session-facing notification port: wake a running Claude session when external state changes.

Three decoupled ports (`base`): a `Source` watches one slice of the `.devloop/` state bus and
builds `Notification`s; a `Notifier` delivers them. Two delivery transports consume the SAME
source, so they never disagree on when to wake:

- `channel` — push into an open session (research preview + `mcp`): `ChannelNotifier` + the
  long-lived `run_channel` runner; content lands inline, multi-wake, set-and-forget.
- `waiter` — a one-shot background task whose exit re-invokes the session (stdlib only):
  `StdoutNotifier` + the `run_waiter` runner; content on the task's stdout, single-wake, re-armed.

`sources/` holds the sources (forge / review today; deploy / verdict later) and the `SOURCES`
registry the dispatcher (`scripts/notify.py`) routes on.
"""
