#!/usr/bin/env bash
# Owner-only operation revocation wrapper for Linux/macOS.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "${SCRIPT_DIR}/revoke.py" "$@"
