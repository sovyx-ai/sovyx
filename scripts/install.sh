#!/usr/bin/env bash
# Sovyx Installer
# Usage: curl -fsSL https://get.sovyx.dev | sh
set -euo pipefail

SOVYX_VERSION="${SOVYX_VERSION:-0.1.0}"

echo "🔮 Installing Sovyx v${SOVYX_VERSION}..."

# Detect OS
OS="$(uname -s)"
ARCH="$(uname -m)"

case "$OS" in
    Linux)  ;;
    Darwin) ;;
    *)
        echo "❌ Unsupported OS: $OS"
        exit 1
        ;;
esac

echo "  OS: $OS ($ARCH)"

# Install uv if not present
if ! command -v uv &> /dev/null; then
    echo "  📦 Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi

# Install sovyx
echo "  📦 Installing sovyx..."
uv tool install "sovyx==${SOVYX_VERSION}"

# Initialize
echo ""
echo "🔮 Sovyx installed! Run:"
echo ""
echo "  sovyx init          # Create default config"
echo "  sovyx start         # Start the daemon"
echo "  sovyx --help        # See all commands"
echo ""
