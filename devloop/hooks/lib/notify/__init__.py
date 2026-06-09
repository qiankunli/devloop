"""Session-facing notification port: deliver a `Notification` to the running Claude session.

`base` defines the abstraction (a `Notification` + the `Notifier` port); `channel` is the
first concrete delivery — a Claude Code channel push. Other backends (a payload-less wake
signal, the one-shot-waiter fallback) can implement the same port. Producers (forge / deploy
/ verdict) build `Notification`s; a `Notifier` delivers them — the two stay decoupled.
"""
