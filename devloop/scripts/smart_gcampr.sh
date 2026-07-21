#!/usr/bin/env bash
# gcampr — stage + commit + push + create/reuse MR. Thin wrapper over commit_flow.py.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$DIR/python" "$DIR/commit_flow.py" mr "$@"
