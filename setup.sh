#!/bin/bash
# Create the virtual environment and install everything Signature Zine needs.
set -e
cd "$(dirname "$0")"
source ./venv-path.sh
VENV="$(resolve_venv)"

PY="${PYTHON:-python3}"
echo "Using $($PY -V) at $(command -v "$PY")"
echo "Environment: $VENV"

if [ ! -x "$VENV/bin/python" ]; then
    mkdir -p "$(dirname "$VENV")"
    "$PY" -m venv "$VENV"
fi

"$VENV/bin/python" -m pip install --upgrade pip >/dev/null
"$VENV/bin/python" -m pip install -r requirements.txt

if [ "$(uname)" = "Darwin" ]; then
    chflags -R nohidden "$VENV" 2>/dev/null || true
fi

echo
echo "Done. Start the application with:  ./run.sh"
