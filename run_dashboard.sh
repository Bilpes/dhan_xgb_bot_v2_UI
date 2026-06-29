#!/usr/bin/env bash
# =============================================================
# run_dashboard.sh — Start the XGB Bot Dashboard
#
# Usage:
#   chmod +x run_dashboard.sh
#   ./run_dashboard.sh
#
# Then open: http://localhost:5050
# =============================================================

set -e

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_ROOT"

echo ""
echo "  ⚡ XGB Bot Dashboard"
echo "  ─────────────────────────────"
echo "  Project: $PROJECT_ROOT"
echo ""

# ── Check .env ──────────────────────────────────────────────
if [ ! -f "config/.env" ]; then
  echo "  ⚠️  config/.env not found!"
  echo "  Copy config/.env.example → config/.env and fill credentials."
  echo ""
fi

# ── Install deps ────────────────────────────────────────────
echo "  📦 Installing dashboard dependencies..."
pip install -q -r ui/api/requirements.txt

echo ""
echo "  ✅ Starting dashboard server..."
echo "  🌐 Open in browser: http://localhost:5050"
echo "  Press Ctrl+C to stop."
echo ""

export PYTHONPATH="$PROJECT_ROOT:$PYTHONPATH"
export FLASK_APP=ui/api/app.py

python -m ui.api.app
