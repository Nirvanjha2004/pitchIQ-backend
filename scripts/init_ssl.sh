#!/usr/bin/env bash
# init_ssl.sh — first-time TLS certificate setup via Let's Encrypt
# Run ONCE after DNS is pointing to this server.
# Usage: ./scripts/init_ssl.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$ROOT_DIR"

if [ ! -f .env ]; then
  echo "ERROR: .env not found. Copy .env.example and fill in your values."
  exit 1
fi

DOMAIN=$(grep -E '^DOMAIN=' .env | cut -d'=' -f2- | tr -d '"' | tr -d "'" | tr -d '[:space:]')
CERTBOT_EMAIL=$(grep -E '^CERTBOT_EMAIL=' .env | cut -d'=' -f2- | tr -d '"' | tr -d "'" | tr -d '[:space:]')

[ -z "$DOMAIN" ]         && echo "ERROR: DOMAIN not set in .env"         && exit 1
[ -z "$CERTBOT_EMAIL" ]  && echo "ERROR: CERTBOT_EMAIL not set in .env"  && exit 1

echo "==> Domain : $DOMAIN"
echo "==> Email  : $CERTBOT_EMAIL"

# Verify DNS resolves before attempting cert (saves a failed attempt)
echo "==> Checking DNS..."
SERVER_IP=$(curl -s http://checkip.amazonaws.com)
DOMAIN_IP=$(dig +short "$DOMAIN" | tail -1)
if [ "$SERVER_IP" != "$DOMAIN_IP" ]; then
  echo "WARNING: $DOMAIN resolves to $DOMAIN_IP but this server is $SERVER_IP"
  echo "         DNS may not have propagated yet. Continue anyway? (y/N)"
  read -r answer
  [ "$answer" != "y" ] && exit 1
fi

echo "==> Starting nginx on port 80 for ACME challenge..."
docker compose up -d nginx
sleep 3

echo "==> Requesting free Let's Encrypt certificate..."
docker compose run --rm certbot

echo "==> Reloading nginx with TLS enabled..."
docker compose exec nginx nginx -s reload

echo ""
echo "==> Done! Your API is live at: https://${DOMAIN}"
echo ""
echo "Add this cron job for auto-renewal (run: crontab -e):"
echo "  0 3 * * * cd $ROOT_DIR && docker compose run --rm certbot renew && docker compose exec nginx nginx -s reload >> /var/log/certbot-renew.log 2>&1"
