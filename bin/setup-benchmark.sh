#!/usr/bin/env bash
set -euo pipefail

# ══════════════════════════════════════════════════════════════
#  setup-benchmark.sh — Clone polyglot-benchmark + build Docker
# ══════════════════════════════════════════════════════════════
#
# Usage:
#   ./bin/setup-benchmark.sh             # Full setup
#   ./bin/setup-benchmark.sh --rebuild   # Rebuild Docker image
#   ./bin/setup-benchmark.sh --update    # git pull + rebuild
#
# What it does:
#   1. Creates benchmark/tmp.benchmarks/
#   2. Clones polyglot-benchmark into benchmark/tmp.benchmarks/
#   3. Creates benchmark/tmp.benchmark/ (results)
#   4. Checks Docker
#   5. Builds Docker image from benchmark/Dockerfile

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

BENCHMARK_DIR="$PROJECT_DIR/benchmark"
BENCHMARKS_DIR="$BENCHMARK_DIR/tmp.benchmarks"
POLYGLOT_DIR="$BENCHMARKS_DIR/polyglot-benchmark"
RESULTS_DIR="$BENCHMARK_DIR/tmp.benchmark"
DOCKER_IMAGE="benchmark-localcode"

# ── Parse CLI arguments ──────────────────────────────────────
REBUILD=false
UPDATE=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --rebuild)
            REBUILD=true
            shift
            ;;
        --update)
            UPDATE=true
            shift
            ;;
        --help|-h)
            echo "Usage: $0 [--rebuild] [--update]"
            echo ""
            echo "Setup benchmark environment: clone polyglot-benchmark + build Docker."
            echo ""
            echo "Options:"
            echo "  --rebuild   Force rebuild Docker image"
            echo "  --update    git pull polyglot + rebuild Docker"
            echo ""
            echo "Directories:"
            echo "  benchmark/tmp.benchmarks/polyglot-benchmark/  Test exercises"
            echo "  benchmark/tmp.benchmark/                      Results"
            exit 0
            ;;
        *)
            echo "Error: Unknown option: $1"
            exit 1
            ;;
    esac
done

echo "══════════════════════════════════════════════"
echo "  Benchmark Setup"
echo "══════════════════════════════════════════════"

# ── 1. Create directories ────────────────────────────────────
echo ""
echo "[1/4] Benchmark directories..."

mkdir -p "$BENCHMARKS_DIR"
mkdir -p "$RESULTS_DIR"
echo "  $BENCHMARKS_DIR"
echo "  $RESULTS_DIR"

# ── 2. Clone or update polyglot-benchmark ────────────────────
echo ""
echo "[2/4] Polyglot benchmark..."

if [[ ! -d "$POLYGLOT_DIR" ]]; then
    echo "  Cloning polyglot-benchmark..."
    git clone https://github.com/Aider-AI/polyglot-benchmark.git "$POLYGLOT_DIR"
elif [[ "$UPDATE" == "true" ]]; then
    echo "  Updating polyglot-benchmark (git pull)..."
    git -C "$POLYGLOT_DIR" pull --ff-only
else
    echo "  Already exists: $POLYGLOT_DIR"
fi

# ── 3. Check Docker ──────────────────────────────────────────
echo ""
echo "[3/4] Docker..."

if ! command -v docker &>/dev/null; then
    echo "  ERROR: Docker not found. Install Docker Desktop first."
    exit 1
fi

if ! docker info &>/dev/null; then
    echo "  ERROR: Docker daemon not running. Start Docker Desktop first."
    exit 1
fi

echo "  Docker is available."

# ── 4. Build Docker image ────────────────────────────────────
echo ""
echo "[4/4] Docker image..."

DOCKERFILE="$BENCHMARK_DIR/Dockerfile"

if [[ ! -f "$DOCKERFILE" ]]; then
    echo "  ERROR: Dockerfile not found: $DOCKERFILE"
    exit 1
fi

if [[ "$REBUILD" == "true" ]] || [[ "$UPDATE" == "true" ]]; then
    echo "  Building Docker image (forced)..."
    docker build -t "$DOCKER_IMAGE" "$BENCHMARK_DIR"
elif docker images --format '{{.Repository}}' | grep -q "^${DOCKER_IMAGE}$"; then
    echo "  Image already exists: $DOCKER_IMAGE"
else
    echo "  Building Docker image..."
    docker build -t "$DOCKER_IMAGE" "$BENCHMARK_DIR"
fi

# ── Done ─────────────────────────────────────────────────────
echo ""
echo "══════════════════════════════════════════════"
echo "  Setup complete"
echo "══════════════════════════════════════════════"
echo "Polyglot:  $POLYGLOT_DIR"
echo "Results:   $RESULTS_DIR"
echo "Docker:    $DOCKER_IMAGE"
echo ""
echo "Next steps:"
echo "  ./bin/start-server.sh <agent> --background"
echo "  ./bin/run-benchmark.sh <agent> -k space-age"
echo "  ./bin/stop-server.sh"
echo "══════════════════════════════════════════════"
