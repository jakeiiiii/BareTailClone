#!/usr/bin/env bash
#
# Build BareTail into a standalone application.
#
# On macOS this produces BareTail.app, which is the form macOS expects: a
# double-clickable bundle needing no Python on the target machine. On Linux it
# produces a single executable file.
#
#   ./build.sh                 build the default for this platform
#   ./build.sh --onedir        a folder rather than one file; already the
#                              default on macOS, where a .app is a directory
#   ./build.sh --console       keep stdout attached, for debugging a build
#   ./build.sh --skip-tests    build without running the tests first
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NAME="BareTail"

ONEDIR=0
CONSOLE=0
SKIP_TESTS=0

while [ $# -gt 0 ]; do
    case "$1" in
        --onedir)     ONEDIR=1 ;;
        --console)    CONSOLE=1 ;;
        --skip-tests) SKIP_TESTS=1 ;;
        -h|--help)    awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}'                           "${BASH_SOURCE[0]}"; exit 0 ;;
        *)            echo "unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done

if [ -t 1 ]; then
    CYAN=$'\033[36m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; GREY=$'\033[90m'; OFF=$'\033[0m'
else
    CYAN=''; GREEN=''; YELLOW=''; GREY=''; OFF=''
fi
step() { printf '%s==> %s%s\n' "$CYAN" "$1" "$OFF"; }
ok()   { printf '%s    %s%s\n' "$GREEN" "$1" "$OFF"; }
warn() { printf '%s    %s%s\n' "$YELLOW" "$1" "$OFF"; }
note() { printf '%s    %s%s\n' "$GREY" "$1" "$OFF"; }
die()  { printf '\nerror: %s\n' "$1" >&2; exit 1; }

cd "$ROOT"

case "$(uname -s)" in
    Darwin) PLATFORM=macos ;;
    Linux)  PLATFORM=linux ;;
    *)      PLATFORM=other ;;
esac

# --- Python -----------------------------------------------------------------
step "Checking Python"
PYTHON="${PYTHON:-python3}"
command -v "$PYTHON" >/dev/null 2>&1 || die "$PYTHON was not found on PATH. Install Python 3.10 or newer."

VERSION="$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
"$PYTHON" - <<'PY' || die "Python 3.10 or newer is required."
import sys
sys.exit(0 if sys.version_info >= (3, 10) else 1)
PY
ok "python $VERSION at $(command -v "$PYTHON")"

"$PYTHON" -c 'import tkinter' >/dev/null 2>&1 || die \
    "tkinter is not available in this Python.
       macOS: install from python.org, or 'brew install python-tk'.
       Linux: install your distribution's python3-tk package."

# The Tk version matters more on macOS than anywhere else. Apple's system Tk
# is 8.5, which has long-standing rendering and event bugs; a Tk application
# built against it looks and behaves noticeably worse. python.org builds and
# Homebrew's python-tk both ship 8.6.
TKVERSION="$("$PYTHON" -c 'import tkinter; print(tkinter.TkVersion)')"
if [ "$(printf '%s\n8.6\n' "$TKVERSION" | sort -g | head -1)" != "8.6" ]; then
    warn "Tk $TKVERSION detected."
    if [ "$PLATFORM" = "macos" ]; then
        warn "This is Apple's system Tk, which renders poorly and has known event bugs."
        warn "Install Python from python.org, or run: brew install python-tk"
    fi
    warn "The build will work, but the result will not look its best."
else
    ok "Tk $TKVERSION"
fi

# --- PyInstaller ------------------------------------------------------------
step "Checking PyInstaller"
if ! "$PYTHON" -c 'import PyInstaller' >/dev/null 2>&1; then
    warn "PyInstaller not found; installing it"
    "$PYTHON" -m pip install --quiet --upgrade pyinstaller \
        || die "Could not install PyInstaller. In a managed environment, try a virtualenv:
       python3 -m venv .venv && source .venv/bin/activate && ./build.sh"
fi
ok "PyInstaller $("$PYTHON" -c 'import PyInstaller; print(PyInstaller.__version__)')"

# --- Tests ------------------------------------------------------------------
if [ "$SKIP_TESTS" -eq 1 ]; then
    warn "Skipping tests (--skip-tests)"
else
    step "Running the test suite"
    "$PYTHON" -m pytest tests/ -q \
        || die "Tests failed. Fix them before building, or pass --skip-tests."
    ok "Tests passed"
fi

# --- Clean ------------------------------------------------------------------
step "Cleaning previous build"
rm -rf build dist ./*.spec
ok "build/, dist/ and *.spec removed"

# --- Build ------------------------------------------------------------------
step "Building"
ARGS=(-m PyInstaller --name "$NAME" --noconfirm --clean)

# A macOS .app is a directory by definition, so one-file mode cannot apply to
# one. PyInstaller warns that the combination "clashes with macOS's security"
# -- it breaks signing and notarisation -- and will reject it outright in
# version 7. Nothing is lost by using onedir here: the .app is still the
# single icon a user drags to Applications.
if [ "$PLATFORM" = "macos" ] && [ "$CONSOLE" -eq 0 ] && [ "$ONEDIR" -eq 0 ]; then
    ONEDIR=1
    note "Building a .app bundle, so using onedir (one-file mode cannot"
    note "produce a bundle, and PyInstaller 7 refuses the combination)."
fi

if [ "$ONEDIR" -eq 1 ]; then ARGS+=(--onedir); else ARGS+=(--onefile); fi
if [ "$CONSOLE" -eq 1 ]; then ARGS+=(--console); else ARGS+=(--windowed); fi

# None of these are used; excluding them keeps the bundle to a sane size.
for module in numpy pandas matplotlib PIL scipy PyQt5 PyQt6 PySide2 PySide6 \
              setuptools pytest unittest pydoc; do
    ARGS+=(--exclude-module "$module")
done

if [ "$PLATFORM" = "macos" ] && [ -f "$ROOT/baretail.icns" ]; then
    ARGS+=(--icon "$ROOT/baretail.icns")
    ok "using baretail.icns"
elif [ -f "$ROOT/baretail.ico" ]; then
    ARGS+=(--icon "$ROOT/baretail.ico")
    ok "using baretail.ico"
fi

ARGS+=(baretail.py)
"$PYTHON" "${ARGS[@]}" || die "PyInstaller failed."

# --- Report -----------------------------------------------------------------
# On macOS a --windowed build also yields a .app bundle, which is the thing
# people actually want to keep.
APP="$ROOT/dist/$NAME.app"
if [ "$ONEDIR" -eq 1 ]; then
    BIN="$ROOT/dist/$NAME/$NAME"
else
    BIN="$ROOT/dist/$NAME"
fi

echo
step "Done"
if [ "$PLATFORM" = "macos" ] && [ -d "$APP" ]; then
    ok "$APP  ($(du -sh "$APP" | cut -f1))"
    [ -f "$BIN" ] && ok "$BIN  ($(du -h "$BIN" | cut -f1))"
    echo
    note "Try it:"
    note "  open -a \"$APP\" --args /path/to/some.log"
    note "  $BIN /path/to/some.log"
    echo
    note "Gatekeeper will refuse an unsigned app downloaded from elsewhere."
    note "Built locally it just runs. If macOS does complain, either"
    note "right-click the app and choose Open, or clear the quarantine flag:"
    note "  xattr -dr com.apple.quarantine \"$APP\""
    echo
    note "The bundle is built for $(uname -m). To ship one binary for both"
    note "Apple Silicon and Intel you need a universal2 Python, then add"
    note "--target-arch universal2 to the PyInstaller arguments above."
else
    [ -f "$BIN" ] || die "Build reported success but $BIN is missing."
    ok "$BIN  ($(du -h "$BIN" | cut -f1))"
    echo
    note "Try it:  $BIN /path/to/some.log"
fi

echo
note "Self-contained: no Python needed on the target machine. Settings are"
note "written beside the executable, so copying it carries your highlight"
note "rules along with it."
if [ "$ONEDIR" -eq 0 ]; then
    echo
    note "A one-file build unpacks itself on every launch, costing a second or"
    note "so of startup. Use --onedir if that matters more than a single file."
fi
