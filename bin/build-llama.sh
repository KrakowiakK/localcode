#!/usr/bin/env bash
set -euo pipefail

# ══════════════════════════════════════════════════════════════
#  build-llama.sh — Clone/update and build llama.cpp from main
# ══════════════════════════════════════════════════════════════
#
# Usage:
#   ./bin/build-llama.sh            # Clone (or pull) + build
#   ./bin/build-llama.sh --update   # Force git pull + rebuild
#   ./bin/build-llama.sh --clean    # Clean build from scratch
#
# Result:
#   bin/llama-server   — ready to use

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
LLAMA_DIR="$PROJECT_DIR/llama.cpp"
BIN_DIR="$SCRIPT_DIR"
BUILD_DIR="$LLAMA_DIR/build"

CLEAN=false
UPDATE=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --clean)
            CLEAN=true
            shift
            ;;
        --update)
            UPDATE=true
            shift
            ;;
        --help|-h)
            echo "Usage: $0 [--update] [--clean]"
            echo ""
            echo "Clone/update and build llama.cpp from main branch."
            echo ""
            echo "Options:"
            echo "  --update   Force git pull before building"
            echo "  --clean    Remove build dir and rebuild from scratch"
            echo ""
            echo "Output:"
            echo "  bin/llama-server"
            exit 0
            ;;
        *)
            echo "Error: Unknown option: $1"
            exit 1
            ;;
    esac
done

echo "══════════════════════════════════════════════"
echo "  Building llama.cpp"
echo "══════════════════════════════════════════════"

# ── Clone or update ──────────────────────────────────────────
if [[ ! -d "$LLAMA_DIR" ]]; then
    echo "Cloning llama.cpp..."
    git clone https://github.com/ggml-org/llama.cpp.git "$LLAMA_DIR"
    UPDATE=false  # just cloned, no need to pull
else
    echo "llama.cpp directory exists: $LLAMA_DIR"
fi

if [[ "$UPDATE" == "true" ]]; then
    echo "Updating llama.cpp (git pull)..."
    git -C "$LLAMA_DIR" pull --ff-only
fi

# Show current commit
COMMIT="$(git -C "$LLAMA_DIR" log --oneline -1)"
echo "Commit: $COMMIT"

# ── Clean if requested ───────────────────────────────────────
if [[ "$CLEAN" == "true" ]] && [[ -d "$BUILD_DIR" ]]; then
    echo "Cleaning build directory..."
    rm -rf "$BUILD_DIR"
fi

# ── Build ────────────────────────────────────────────────────
mkdir -p "$BUILD_DIR"

echo "Configuring with CMake..."
cmake -S "$LLAMA_DIR" -B "$BUILD_DIR" \
    -DCMAKE_BUILD_TYPE=Release \
    -DGGML_METAL=ON \
    -DLLAMA_CURL=ON

echo "Building..."
NPROC="$(sysctl -n hw.ncpu 2>/dev/null || nproc 2>/dev/null || echo 4)"
cmake --build "$BUILD_DIR" --config Release -j "$NPROC" --target llama-server

# ── Install binary ───────────────────────────────────────────
mkdir -p "$BIN_DIR"
cp "$BUILD_DIR/bin/llama-server" "$BIN_DIR/llama-server"
chmod +x "$BIN_DIR/llama-server"

# ── Verify ───────────────────────────────────────────────────
VERSION="$("$BIN_DIR/llama-server" --version 2>&1 | head -1 || echo "unknown")"

echo ""
echo "══════════════════════════════════════════════"
echo "  Build complete"
echo "══════════════════════════════════════════════"
echo "Binary:  $BIN_DIR/llama-server"
echo "Version: $VERSION"
echo "Commit:  $COMMIT"
echo "══════════════════════════════════════════════"
