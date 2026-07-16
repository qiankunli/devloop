"""Session-facing notification port: wake a running Claude session when external state changes.

Three decoupled ports (`base`): a `Source` watches one slice of the `.devloop/` state bus and
builds `Notification`s; a `Notifier` delivers them. Two delivery transports consume the SAME
source, so they never disagree on when to wake:

- `channel` — push into an open session (research preview + `mcp`): `ChannelNotifier` + the
  long-lived `run_channel` runner; content lands inline, multi-wake, set-and-forget.
- `waiter` — a one-shot background task whose exit re-invokes the session (stdlib only):
  `StdoutNotifier` + the `run_waiter` runner; content on the task's stdout, single-wake, re-armed.

`sources/` holds the sources (forge / review today; deploy / verdict later) and the `SOURCES`
registry the dispatcher (`scripts/notify.py`) routes on. `CompositeSource` (token `all`) fans over
every leaf so one transport can watch the whole bus, agnostic to which source fired.

The dispatcher's `should-arm` verb is the capability decision, run FIRST (synchronous, non-waking):
exit 0 → the caller arms a `waiter`; exit 1 → a standing `channel all` already covers the session,
skip. Keeping the decision OUT of the backgrounded process is what lets a standing channel cost zero
arming (a backgrounded decider would itself wake on exit). So producers and the woken turn never
name a transport.
"""
