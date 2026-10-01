#!/bin/bash
# Build Signature Zine as a standalone application that needs no Python
# installed to run.
#
#   packaging/build.sh              build for this machine
#   packaging/build.sh --dmg        macOS: also wrap the .app in a disk image
#   packaging/build.sh --universal  macOS: build for Intel and Apple Silicon
#
# Output lands in dist/. Nothing here needs a developer account or a
# certificate; see the README for what to tell macOS the first time you
# open an unsigned app.
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$PWD"

DMG=0
UNIVERSAL=0
for arg in "$@"; do
    case "$arg" in
        --dmg) DMG=1 ;;
        --universal) UNIVERSAL=1 ;;
        -h|--help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

# Build in its own environment so the app never picks up whatever happens to
# be installed for day-to-day work.
BUILD_VENV="${BUILD_VENV:-$ROOT/.venv-build}"
if [ ! -x "$BUILD_VENV/bin/python" ]; then
    echo "==> creating build environment in $BUILD_VENV"
    python3 -m venv "$BUILD_VENV"
fi
PY="$BUILD_VENV/bin/python"

echo "==> installing build dependencies"
"$PY" -m pip install --quiet --upgrade pip
"$PY" -m pip install --quiet -r requirements.txt "pyinstaller>=6.6"

echo "==> drawing the icon"
"$PY" packaging/make_icon.py

echo "==> checking the tests pass before packaging anything"
"$PY" -m unittest discover -s tests -q

rm -rf build dist
EXTRA=()
if [ "$UNIVERSAL" = "1" ]; then
    if [ "$(uname -s)" != "Darwin" ]; then
        echo "--universal only means anything on macOS" >&2; exit 2
    fi
    # Only possible when every wheel in the environment carries both
    # architectures; PyInstaller says plainly which one does not.
    EXTRA+=(--target-arch universal2)
fi

echo "==> running PyInstaller"
"$PY" -m PyInstaller --noconfirm --clean "${EXTRA[@]}" \
    --distpath dist --workpath build \
    packaging/SignatureZine.spec

case "$(uname -s)" in
Darwin)
    APP="dist/Signature Zine.app"
    [ -d "$APP" ] || { echo "no .app was produced" >&2; exit 1; }
    # An ad-hoc signature is not a real one, but without it macOS on Apple
    # Silicon refuses to launch the bundle at all.
    echo "==> ad-hoc signing"
    codesign --force --deep --sign - "$APP" || \
        echo "    (codesign failed; the app may refuse to open)"
    echo "==> built $APP"
    if [ "$DMG" = "1" ]; then
        echo "==> building the disk image"
        STAGE="$(mktemp -d)"
        cp -R "$APP" "$STAGE/"
        ln -s /Applications "$STAGE/Applications"
        hdiutil create -volname "Signature Zine" -srcfolder "$STAGE" \
            -ov -format UDZO "dist/Signature Zine.dmg" >/dev/null
        rm -rf "$STAGE"
        echo "==> built dist/Signature Zine.dmg"
    fi
    ;;
Linux)
    echo "==> built dist/signature-zine/ (run dist/signature-zine/signature-zine)"
    ;;
*)
    echo "==> built dist/"
    ;;
esac

du -sh dist/* 2>/dev/null || true
