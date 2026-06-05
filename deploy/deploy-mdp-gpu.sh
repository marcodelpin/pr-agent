#!/usr/bin/env bash
# Deploy PR-Agent Review Server to mdp-gpu-wsl
# =============================================
#
# This script:
#   1. Copies the pr-agent repo to mdp-gpu-wsl
#   2. Builds the Docker image
#   3. Starts the container
#   4. Configures nginx on LXC 220
#   5. Adds DNS record for pr-review.mdp
#
# Prerequisites:
#   - SSH access to mdp-gpu-wsl
#   - Docker installed on mdp-gpu-wsl
#   - Access to LXC 220 for nginx/DNS config
#
# Usage:
#   ./deploy-mdp-gpu.sh

set -euo pipefail

# Configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
REMOTE_HOST="mdp-gpu-wsl"
REMOTE_DIR="/home/mdelpin/pr-agent"
PROXMOX_HOST="192.168.71.6"
LXC_ID="220"

echo "=== PR-Agent Review Server Deployment ==="
echo "Source: $REPO_DIR"
echo "Target: $REMOTE_HOST:$REMOTE_DIR"
echo ""

# Step 1: Sync repo to mdp-gpu-wsl
echo "[1/5] Syncing repository to $REMOTE_HOST..."
rsync -avz --delete \
    --exclude '.git' \
    --exclude '.venv' \
    --exclude '__pycache__' \
    --exclude '*.pyc' \
    --exclude '.pytest_cache' \
    "$REPO_DIR/" "$REMOTE_HOST:$REMOTE_DIR/"
echo "✓ Repository synced"

# Step 2: Build Docker image on remote
echo ""
echo "[2/5] Building Docker image on $REMOTE_HOST..."
ssh "$REMOTE_HOST" << 'EOF'
cd ~/pr-agent
docker build -f docker/Dockerfile.local-review -t pr-agent-review:latest .
EOF
echo "✓ Docker image built"

# Step 3: Start container
echo ""
echo "[3/5] Starting container..."
ssh "$REMOTE_HOST" << 'EOF'
cd ~/pr-agent

# Stop existing container if running
docker stop pr-review 2>/dev/null || true
docker rm pr-review 2>/dev/null || true

# Start new container
docker run -d \
    --name pr-review \
    --restart unless-stopped \
    -p 8080:8080 \
    -e OLLAMA_BASE_URL=http://172.17.0.1:11434 \
    -e PR_AGENT_MODEL=ollama/qwen3-coder:30b \
    pr-agent-review:latest

# Wait for startup
sleep 3

# Check health
if curl -sf http://localhost:8080/health > /dev/null; then
    echo "✓ Container healthy"
else
    echo "✗ Container not healthy"
    docker logs pr-review --tail 20
    exit 1
fi
EOF
echo "✓ Container started"

# Step 4: Configure nginx on LXC 220
echo ""
echo "[4/5] Configuring nginx on LXC 220..."
scp "$SCRIPT_DIR/nginx-pr-review.conf" "root@$PROXMOX_HOST:/tmp/"
ssh "root@$PROXMOX_HOST" << EOF
# Copy nginx config to LXC
pct push $LXC_ID /tmp/nginx-pr-review.conf /etc/nginx/sites-available/pr-review.mdp

# Enable site
pct exec $LXC_ID -- ln -sf /etc/nginx/sites-available/pr-review.mdp /etc/nginx/sites-enabled/

# Test and reload nginx
pct exec $LXC_ID -- nginx -t && pct exec $LXC_ID -- systemctl reload nginx
EOF
echo "✓ Nginx configured"

# Step 5: Add DNS record
echo ""
echo "[5/5] Adding DNS record for pr-review.mdp..."
ssh "root@$PROXMOX_HOST" << EOF
# Check if record already exists
if pct exec $LXC_ID -- grep -q "pr-review" /etc/bind/zones/db.mdp; then
    echo "DNS record already exists"
else
    # Add A record (points to LXC 220 proxy)
    pct exec $LXC_ID -- sed -i '/; AI Services/a pr-review\tIN\tA\t192.168.71.220' /etc/bind/zones/db.mdp

    # Increment serial
    pct exec $LXC_ID -- sed -i 's/\([0-9]\{10\}\)/echo \$((\1+1))/e' /etc/bind/zones/db.mdp

    # Reload DNS
    pct exec $LXC_ID -- rndc reload
    echo "✓ DNS record added"
fi
EOF

echo ""
echo "=== Deployment Complete ==="
echo ""
echo "Service available at:"
echo "  - https://pr-review.mdp (via proxy)"
echo "  - http://100.124.180.114:8080 (direct)"
echo ""
echo "Test commands:"
echo "  curl -sk https://pr-review.mdp/health"
echo "  curl -sk https://pr-review.mdp/models"
echo "  git diff HEAD~1 | curl -sk -X POST https://pr-review.mdp/review/raw -d @-"
