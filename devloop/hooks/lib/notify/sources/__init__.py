"""Wake sources for the notify port: each watches one slice of the `.devloop/` state bus and
decides when to fire. Both transports (channel push / waiter exit) consume these unchanged.

`SOURCES` is the registry the dispatcher (`scripts/notify.py`) routes on — the single place that
maps a source name to its `Source`, so adding a deploy/verdict source = one entry here + one module.
"""
from lib.notify.sources.forge import ForgeSource
from lib.notify.sources.review import ReviewSource

SOURCES = {
    "forge": ForgeSource(),
    "review": ReviewSource(),
}
