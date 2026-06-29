"""Wake sources for the notify port: each watches one slice of the `.devloop/` state bus and
decides when to fire. Both transports (channel push / waiter exit) consume these unchanged.

`SOURCES` is the registry the dispatcher (`scripts/notify.py`) routes on — the single place that
maps a source name to its `Source`. A deploy/verdict source = one entry in `_LEAVES` + one module;
`all` (CompositeSource) auto-covers it, so a `channel all` / `waiter all` watch needs no change.
"""
from lib.notify.sources.composite import CompositeSource
from lib.notify.sources.forge import ForgeSource
from lib.notify.sources.review import ReviewSource

# Leaf sources: each watches one segment of the bus.
_LEAVES = {
    "forge": ForgeSource(),
    "review": ReviewSource(),
}

# `all` composes the leaves so one transport can watch the whole bus (see composite.py). Listed
# last so its token never shadows a leaf name.
SOURCES = {**_LEAVES, "all": CompositeSource(_LEAVES)}
