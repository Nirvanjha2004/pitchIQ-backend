#!/usr/bin/env bash
# init_ssl.sh — first-time TLS certificate setup via Let's Encrypt
# Run ONCE after DNS is pointing to this server.
# Usage: ./scripts/init_ssl.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$ROOT_DIR"

if [ ! -f .env ]; then
  echo "ERROR: .env not found."
  exit 1
fi

DOMAIN=$(grep -E '^DOMAIN=' .env | cut -d'=' -f2- | tr -d '"' | tr -d "'" | tr -d '[:space:]')
CERTBOT_EMAIL=$(grep -E '^CERTBOT_EMAIL=' .env | cut -d'=' -f2- | tr -d '"' | tr -d "'" | tr -d '[:space:]')

[ -z "$DOMAIN" ]        && echo "ERROR: DOMAIN not set in .env"        && exit 1
[ -z "$CERTBOT_EMAIL" ] && echo "ERROR: CERTBOT_EMAIL not set in .env" && exit 1

echo "==> Domain : $DOMAIN"
echo "==> Email  : $CERTBOT_EMAIL"

# Step 1: start nginx with HTTP-only config (no SSL block = no missing cert error)
echo "==> Starting nginx (HTTP only)..."
docker compose up -d nginx
sleep 3

# Verify nginx is actually up
if ! docker compose exec nginx nginx -t 2>/dev/null; then
  echo "ERROR: nginx config test failed"
  docker compose logs nginx
  exit 1
fi

echo "==> nginx is up. Requesting certificate..."
docker compose run --rm certbot

# Step 2: uncomment the HTTPS server block now that the cert exists
CONF="nginx/conf.d/pitchiq.conf"
echo "==> Enabling HTTPS block in nginx config..."
sed -i 's/^# server {$/server {/' "$CONF"
sed -i 's/^#     /    /' "$CONF"
sed -i 's/^# }$/}/' "$CONF"
# Remove the comment header lines
sed -i '/^# This block is commented out/d' "$CONF"
sed -i '/^# After the cert is issued/d' "$CONF"

echo "==> Reloading nginx with HTTPS enabled..."
docker compose exec nginx nginx -s reload

echo ""
echo "==> Done! Your API is live at: https://${DOMAIN}"
echo "    Test: curl https://${DOMAIN}/health"
echo ""
echo "Add this cron job for auto-renewal (run: crontab -e):"
echo "  0 3 * * * cd $ROOT_DIR && docker compose run --rm certbot renew && docker compose exec nginx nginx -s reload >> /var/log/certbot-renew.log 2>&1"
