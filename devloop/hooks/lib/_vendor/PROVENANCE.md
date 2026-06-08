# Vendored third-party code

Files here are copied **verbatim** from upstream and **never hand-edited** — to upgrade,
re-vendor from source and update this file. They are isolated from devloop's own code; the
adapters that consume them live elsewhere (e.g. `lib/cmdtree/parable.py`).

## parable.py

- **Project**: Parable — a recursive-descent bash parser (one file, zero deps).
- **Source**: https://github.com/ldayton/Parable — `src/parable.py` (branch `main`).
- **License**: MIT — see [`LICENSE-parable`](./LICENSE-parable). MIT is permissive (no
  copyleft), so devloop stays permissively licensed. (The more-mature `bashlex` was rejected
  for vendoring: it is **GPL-3.0**, which would relicense devloop.)
- **Retrieved**: 2026-06-08.
- **sha256**: `c5b1b1cc72910db56e2eaf2ccb49f97e48216a7f080e42be200bd390d36e7538`
- **Used by**: `lib/cmdtree/parable.py` (the Parable→`cmdtree.base` backend), behind the
  swappable parser seam in `lib/cmdtree/cmdparse.py`.
