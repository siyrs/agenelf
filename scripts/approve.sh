#!/usr/bin/env bash
# Compatibility wrapper. The implementation is Python so Windows, macOS and Linux
# produce exactly the same fingerprint-bound decision document.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "${SCRIPT_DIR}/approve.py" "$@"
