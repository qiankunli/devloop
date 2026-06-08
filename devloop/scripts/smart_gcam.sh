#!/usr/bin/env bash
# gcam — stage + commit (no push). Thin wrapper over smart_git_ops.py.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$DIR/smart_git_ops.py" commit "$@"
