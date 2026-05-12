#!/usr/bin/env bash
# deploy.sh — zero-downtime redeploy on the EC2 instance
# Usage: ./scripts/deploy.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$ROOT_DIR"

echo "==> Pulling latest code..."
git pull origin main

echo "==> Building image..."
docker compose build --no-cache backend

echo "==> Running database migrations..."
docker compose run --rm backend alembic upgrade head

echo "==> Restarting backend with zero downtime..."
docker compose up -d --no-deps backend

echo "==> Cleaning up old images..."
docker image prune -f

echo "==> Done. Container status:"
docker compose ps
