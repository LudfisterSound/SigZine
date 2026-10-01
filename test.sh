#!/bin/bash
cd "$(dirname "$0")"
source ./venv-path.sh
exec "$(resolve_venv)/bin/python" -m unittest discover -s tests "$@"
