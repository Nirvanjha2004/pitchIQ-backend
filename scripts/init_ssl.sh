#!/usr/bin/env bash
# init_ssl.sh — first-time TLS certificate setup via Let's Encrypt
# Run this ONCE after pointing your domain DNS to the EC2 IP.
# Usage: ./scripts/init_ssl.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$ROOT_DIR"

if [ ! -f .env ]; then
  echo "ERROR: .env file not found. Copy .env.example and fill in your values."
  exit 1
fi

# Load only the two vars we need, safely (avoids issues with JSON values in .env)
DOMAIN=$(grep -E '^DOMAIN=' .env | cut -d '=' -f2- | tr -d '"' | tr -d "'")
CERTBOT_EMAIL=$(grep -E '^CERTBOT_EMAIL=' .env | cut -d '=' -f2- | tr -d '"' | tr -d "'")

if [ -z "$DOMAIN" ]; then
  echo "ERROR: DOMAIN is not set in .env"
  exit 1
fi

if [ -z "$CERTBOT_EMAIL" ]; then
  echo "ERROR: CERTBOT_EMAIL is not set in .env"
  exit 1
fi

echo "==> Domain:  $DOMAIN"
echo "==> Email:   $CERTBOT_EMAIL"

echo "==> Starting nginx (HTTP only) for ACME challenge..."
docker compose up -d nginx

echo "==> Waiting for nginx to be ready..."
sleep 3

echo "==> Requesting certificate for ${DOMAIN}..."
docker compose run --rm \
  -e DOMAIN="$DOMAIN" \
  -e CERTBOT_EMAIL="$CERTBOT_EMAIL" \
  certbot

echo "==> Reloading nginx to pick up the new certificate..."
docker compose exec nginx nginx -s reload

echo ""
echo "==> TLS setup complete!"
echo ""
echo "Add this cron job to auto-renew (run: crontab -e):"
echo "  0 3 * * * cd $ROOT_DIR && docker compose run --rm -e DOMAIN=$DOMAIN -e CERTBOT_EMAIL=$CERTBOT_EMAIL certbot renew && docker compose exec nginx nginx -s reload >> /var/log/certbot-renew.log 2>&1"
