#!/bin/bash
# Where the virtual environment lives.
#
# Not inside the project: this folder is very often under ~/Documents, which
# on a Mac is usually synced to iCloud Drive. iCloud would happily upload a
# gigabyte of Qt, and it sets the BSD 'hidden' flag on files while it manages
# them, which stops Qt from finding its own plugins and the application from
# starting at all.
resolve_venv() {
    if [ -n "$SIGZINE_VENV" ]; then
        echo "$SIGZINE_VENV"
    elif [ -x "./venv/bin/python" ]; then
        echo "./venv"
    elif [ "$(uname)" = "Darwin" ]; then
        echo "$HOME/Library/Application Support/Signature Zine/venv"
    else
        echo "./venv"
    fi
}
