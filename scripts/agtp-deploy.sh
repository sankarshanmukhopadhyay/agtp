#!/bin/bash
# =============================================================================
# agtp-deploy
# =============================================================================
# Pull latest from main, restart AGTP services, report status.
# Run on the VPS as root (or via sudo). Idempotent — running it twice
# in a row when there are no changes is safe.
#
# This script lives in the repo at scripts/agtp-deploy.sh and is symlinked
# to /usr/local/bin/agtp-deploy on the VPS during initial setup.
# =============================================================================

set -e

REPO_DIR="/opt/agtp"
SERVICES=("agtp-registry" "agtp-agent")

echo "▶ Deploying AGTP..."
echo "  Repo:     $REPO_DIR"
echo "  Services: ${SERVICES[*]}"
echo

# -----------------------------------------------------------------------------
# 1. Pull latest from main
# -----------------------------------------------------------------------------
cd "$REPO_DIR"

# Stash any local changes (shouldn't exist on the VPS, but defensive)
if ! git diff-index --quiet HEAD --; then
    echo "⚠ Uncommitted local changes detected — stashing"
    git stash push -m "agtp-deploy auto-stash $(date -u +%Y-%m-%dT%H:%M:%SZ)" || true
fi

echo "▶ Fetching latest from origin/main..."
git fetch origin main
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)

if [ "$LOCAL" = "$REMOTE" ]; then
    echo "  Already at latest commit ($LOCAL)"
    NEED_RESTART=0
else
    echo "  Local:  $LOCAL"
    echo "  Remote: $REMOTE"
    git pull origin main
    echo "▶ Reinstalling package..."
    cd "$REPO_DIR"
    pip install -e . --break-system-packages --quiet 2>/dev/null || pip install -e . --quiet
    NEED_RESTART=1
fi

# -----------------------------------------------------------------------------
# 2. Restart services if anything changed
# -----------------------------------------------------------------------------
if [ "$NEED_RESTART" -eq 1 ]; then
    echo
    echo "▶ Restarting services..."
    for svc in "${SERVICES[@]}"; do
        if systemctl is-enabled --quiet "$svc" 2>/dev/null; then
            echo "  Restarting $svc"
            systemctl restart "$svc"
        else
            echo "  ⚠ $svc is not enabled — skipping"
        fi
    done

    # Brief pause to let services come up
    sleep 2

    echo
    echo "▶ Service status:"
    for svc in "${SERVICES[@]}"; do
        if systemctl is-active --quiet "$svc" 2>/dev/null; then
            echo "  ✓ $svc is running"
        else
            echo "  ✗ $svc failed to start — check 'journalctl -u $svc -n 50'"
            exit 1
        fi
    done
else
    echo
    echo "▶ No changes — services not restarted"
fi

# -----------------------------------------------------------------------------
# 3. Report
# -----------------------------------------------------------------------------
echo
echo "✓ Deploy complete — $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "  Commit: $(git rev-parse --short HEAD)"
echo "  Branch: $(git rev-parse --abbrev-ref HEAD)"
