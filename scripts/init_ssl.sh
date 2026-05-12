#!/usr/bin/env bash
# init_ssl.sh — first-time TLS certificate setup via Let's Encrypt
# Run this ONCE after pointing your domain DNS to the EC2 IP.
# Usage: ./scripts/init_ssl.sh
set -euo pipefail

if [ ! -f .env ]; then
  echo "ERROR: .env file not found. Copy .env.example and fill in your values."
  exit 1
fi

source .env

echo "==> Starting nginx (HTTP only) for ACME challenge..."
docker compose up -d nginx

echo "==> Requesting certificate for ${DOMAIN}..."
docker compose run --rm certbot

echo "==> Reloading nginx to pick up the new certificate..."
docker compose exec nginx nginx -s reload

echo "==> TLS setup complete. Add a cron job to auto-renew:"
echo "    0 3 * * * cd $(pwd) && docker compose run --rm certbot renew && docker compose exec nginx nginx -s reload"
