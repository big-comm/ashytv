#!/usr/bin/env bash
# Runs AshyTV from the source tree (development mode).
set -euo pipefail
cd "$(dirname "$0")"
export PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m ashytv "$@"
