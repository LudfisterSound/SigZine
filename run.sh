#!/bin/bash
# Start Signature Zine.
cd "$(dirname "$0")"
source ./venv-path.sh
VENV="$(resolve_venv)"

if [ ! -x "$VENV/bin/python" ]; then
    echo "No virtual environment yet - running setup.sh first."
    ./setup.sh || exit 1
fi
if [ "$(uname)" = "Darwin" ]; then
    # Clear the 'hidden' flag macOS sometimes leaves on these files; Qt cannot
    # enumerate a hidden plugin directory and refuses to start.
    chflags -R nohidden "$VENV" 2>/dev/null || true
fi
exec "$VENV/bin/python" -m sigzine "$@"
