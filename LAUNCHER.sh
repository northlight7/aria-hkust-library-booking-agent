#!/bin/bash
# LAUNCHER.sh — Double-click this file to start Aria on macOS or Linux.
# It opens http://localhost:8091.
#
# On a brand new computer this installs everything Aria needs by itself:
# the package manager, Python, the libraries, and the sign-in browser.
# Nothing is installed system-wide. Everything lives inside this folder,
# so deleting the folder removes it all.
#
# HKUST AI Literacy Course — Path A: Agentic AI Exemplar

cd "$(dirname "$0")"
ROOT="$(pwd)"
UV_DIR="$ROOT/.uv"
UV_BIN="$UV_DIR/uv"
STAMP="$UV_DIR/.chromium-installed"

# Everything Aria downloads lives inside this folder. Python, the voice
# model, the sign-in browser, uv itself — deleting the folder removes it all.
export UV_PYTHON_INSTALL_DIR="$ROOT/.uv/python"
# uv also keeps a package cache and a tool dir. Left alone those go to
# shared per-user directories that survive deleting this folder and reach
# hundreds of megabytes, which contradicts what the README promises.
export UV_CACHE_DIR="$ROOT/.uv/cache"
export UV_TOOL_DIR="$ROOT/.uv/tools"
export UV_TOOL_BIN_DIR="$ROOT/.uv/tools/bin"
export PLAYWRIGHT_BROWSERS_PATH="$ROOT/.uv/playwright-browsers"

say()  { echo "  $*"; }
fail() { echo; say "$1"; echo; read -p "Press Enter to close..."; exit 1; }

# ── 1. Find uv, or install it into this folder ────────────────────────
# uv is a single binary that needs no Python of its own. It can download
# Python, so it is the only thing that has to exist before anything else.
if [ -x "$UV_BIN" ]; then
    UV="$UV_BIN"
elif command -v uv &>/dev/null; then
    UV="$(command -v uv)"
else
    echo
    say "First run: setting up Aria. This takes a few minutes and needs"
    say "an internet connection. You only have to do this once."
    echo
    say "Step 1 of 3: downloading the setup tool..."
    if command -v curl &>/dev/null; then
        curl -LsSf https://astral.sh/uv/install.sh \
            | env UV_UNMANAGED_INSTALL="$UV_DIR" sh >/dev/null 2>&1
    elif command -v wget &>/dev/null; then
        wget -qO- https://astral.sh/uv/install.sh \
            | env UV_UNMANAGED_INSTALL="$UV_DIR" sh >/dev/null 2>&1
    else
        fail "Cannot download anything: this computer has neither curl nor wget."
    fi
    [ -x "$UV_BIN" ] || fail "Setup tool failed to download. Check your internet connection and try again."
    UV="$UV_BIN"
fi

cd app

# ── 2. Python and the libraries ───────────────────────────────────────
# uv reads pyproject.toml, downloads Python 3.11+ if this computer has
# none, creates .venv, and installs the pinned versions from uv.lock.
say "Step 2 of 3: installing Python and the libraries..."
if ! "$UV" sync; then
    fail "Could not install Python or the libraries. Check your internet connection and try again."
fi

# ── 3. The sign-in browser ────────────────────────────────────────────
# Only Chromium. booking_agent.py always launches Chromium for the HKUST
# sign-in, so WebKit and Firefox would be a wasted download.
if [ ! -f "$STAMP" ]; then
    say "Step 3 of 3: installing the sign-in browser (about 150 MB)..."
    if "$UV" run playwright install chromium; then
        touch "$STAMP"
    else
        say "The sign-in browser did not install. Checking room availability"
        say "will still work. Booking needs sign-in, so it will not."
    fi
fi

# ── 4. Open the browser once the server is actually answering ─────────
# Waits for the server, then gives the Kokoro voice model a grace period
# to finish downloading (337 MB on first run). If the voice is not ready
# in time we open anyway: the page falls back to the browser's built-in
# voice and switches to Kokoro on its own once it lands.
( if command -v curl &>/dev/null; then
    READY=0
    for i in $(seq 1 600); do
      if curl -sf -m 2 -o /dev/null "http://localhost:8091/api/session" 2>/dev/null; then
        if curl -sf -m 2 "http://localhost:8091/api/tts/status" 2>/dev/null | grep -q '"ready": true'; then
          break
        fi
        READY=$((READY + 1))
        [ "$READY" -ge 60 ] && break
      fi
      sleep 1
    done
    sleep 1
  else
    sleep 8
  fi
  if command -v open &>/dev/null; then open http://localhost:8091
  elif command -v xdg-open &>/dev/null; then xdg-open http://localhost:8091
  fi ) &

# ── 5. Run Aria ───────────────────────────────────────────────────────
"$UV" run python server.py
EXIT=$?

if [ $EXIT -ne 0 ]; then
    echo
    echo "Aria closed with an error (code $EXIT)."
    echo "If this keeps happening, ask your instructor for help."
    read -p "Press Enter to close..."
fi
exit $EXIT
