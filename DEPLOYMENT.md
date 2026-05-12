# Deploying PitchIQ Backend to AWS t3.micro

## Prerequisites

| Requirement | Notes |
|---|---|
| AWS EC2 t3.micro | Ubuntu 22.04 LTS recommended |
| Elastic IP | Attach one so the IP doesn't change on reboot |
| Domain / subdomain | e.g. `api.yourdomain.com` — point its A record to the Elastic IP |
| Supabase project | Provides PostgreSQL (Transaction pooler URL) |
| Upstash Redis | Provides Redis (TLS `rediss://` URL) |

---

## 1. Provision the EC2 instance

```bash
# Security group inbound rules needed:
# 22   (SSH)   — your IP only
# 80   (HTTP)  — 0.0.0.0/0  (needed for ACME challenge)
# 443  (HTTPS) — 0.0.0.0/0
```

---

## 2. Install Docker on the instance

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
newgrp docker          # apply group without logout
docker --version       # verify
```

---

## 3. Clone the repo and configure environment

```bash
git clone https://github.com/your-org/pitchiq-backend.git
cd pitchiq-backend

cp .env.example .env
nano .env              # fill in all values, especially DOMAIN and CERTBOT_EMAIL
```

---

## 4. Obtain TLS certificate (first time only)

```bash
chmod +x scripts/init_ssl.sh
./scripts/init_ssl.sh
```

This starts Nginx on port 80, runs Certbot, then reloads Nginx with HTTPS.

---

## 5. Start all services

```bash
docker compose up -d
docker compose ps      # all containers should show "healthy" / "running"
```

---

## 6. Run database migrations

```bash
docker compose run --rm backend alembic upgrade head
```

---

## 7. Verify

```bash
curl https://api.yourdomain.com/health
# Expected: {"status":"healthy","service":"pitchiq"}
```

---

## Redeploying after code changes

```bash
./scripts/deploy.sh
```

---

## Auto-renew TLS certificates

Add this cron job on the EC2 instance (`crontab -e`):

```
0 3 * * * cd /home/ubuntu/pitchiq-backend && docker compose run --rm certbot renew && docker compose exec nginx nginx -s reload >> /var/log/certbot-renew.log 2>&1
```

---

## Memory considerations (t3.micro = 1 GiB RAM)

- The backend container is capped at **768 MB**.
- Use **1 Uvicorn worker** (already set in `Dockerfile`).
- Supabase and Upstash are external — no local DB/Redis memory overhead.
- Enable EC2 swap as a safety net:

```bash
sudo fallocate -l 1G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

---

## Logs

```bash
docker compose logs -f backend    # FastAPI logs
docker compose logs -f nginx      # Nginx access / error logs
```
