# Deploying to AWS (near-free tier)

How to get this running on the cheapest realistic AWS setup, with
GitHub Actions redeploying automatically on every push to `main`.

## Cost reality check

"Free" here means the AWS Free Tier's 750 EC2 hours/month (12 months for
a new account) — not literally $0:

- **EC2 `t2.micro`/`t3.micro`, Ubuntu**: free for 750 hrs/month under the
  Free Tier (one instance running 24/7 fits inside that).
- **Public IPv4 address**: AWS has charged for these since Feb 2024 —
  about **$0.005/hr (~$3.60/month)**, whether it's an Elastic IP or just
  the instance's default public IP. There's no way around this if you
  want it reachable from the internet at all. This is the one real,
  ongoing cost.
- **EBS storage**: 30GB free tier covers a small instance comfortably.
- **Data transfer out**: 100GB/month free (was reduced from 15GB in late
  2024) — plenty for a low-traffic personal tool.

So: **~$3.60/month**, not $0, and $0 only for the first 12 months of
compute if you're on a new AWS account. After 12 months, add the
instance's hourly cost too (t3.micro is roughly $7-8/month on-demand).

## Architecture

GitHub Actions doesn't build the image itself or push to a registry —
it `rsync`s the repo to the EC2 box, then SSHes in to build the Docker
images and restart the containers **on the instance**. No registry, no
registry secrets, no separate deploy key: just one SSH keypair.

```
push to main → GitHub Actions → rsync code to EC2 → ssh: docker compose up -d --build
```

Two containers run via `docker-compose.yml`: `app` (this Python service,
not directly reachable from outside) and `caddy` (reverse proxy on 80/443
— see "HTTPS" below for what it does with TLS). Only Caddy is ever
reachable from the internet.

## HTTPS

There's no domain here, so this uses a **self-signed** certificate
rather than a Let's-Encrypt one (a public CA can't issue a trusted cert
for a bare IP). The deploy workflow generates it once with `openssl`
(the instance's public IP baked in as the certificate's Subject
Alternative Name — what browsers actually check for an IP-address URL)
and stores it in `~/data_scrapper_certs/` on the box, outside the repo
directory so it survives every redeploy unchanged. `Caddyfile` points at
that file directly, rather than using Caddy's `tls internal` — that
mode's automatic certificate issuance is hostname-based and has nothing
to match against a bare-IP connection, which fails the handshake outright
rather than falling back to "just serve something."

Traffic is genuinely encrypted, but every browser will still show a
"certificate not trusted" warning once per device (inherent to
self-signed certs, unrelated to the above) — click through it
("Advanced" → "Proceed", wording varies by browser). Plain `http://`
still works but redirects to `https://` rather than serving unencrypted.

If the instance's public IP ever changes (e.g. stopped/restarted without
an Elastic IP), the certificate's SAN won't match it anymore — delete
`~/data_scrapper_certs/` on the box and redeploy to regenerate it for the
new IP.

If you'd rather have a real no-warning padlock, that requires an actual
hostname pointing at the instance (even a free one like a
`sslip.io` subdomain works with Let's Encrypt) — ask if you want that
swapped in later; it's a `Caddyfile` change back to ACME-based issuance.

---

## 1. Launch the EC2 instance

1. AWS Console → EC2 → **Launch instance**
2. AMI: **Ubuntu Server 24.04 LTS** (or latest LTS)
3. Instance type: **t3.micro** (or `t2.micro` if `t3` isn't Free-Tier
   eligible in your account/region)
4. Key pair: create a new one, download the `.pem` file — you'll need its
   contents for a GitHub secret below. **This is the only copy; AWS
   won't let you download it again.**
5. Network settings → Edit security group rules:
   - **SSH (22)**: source `0.0.0.0/0`. GitHub Actions runners use
     ephemeral IPs from a large, changing range, so pinning this to "my
     IP" would break automated deploys. Key-based auth only (Ubuntu
     disables SSH passwords by default) makes this an accepted tradeoff
     for a single small box — it's the standard pattern for CI-to-a-VM
     deploys.
   - **HTTP (80)**: source `0.0.0.0/0` — this is fine because `/api/*`
     is gated by `API_KEY` (see step 3) and everything else served is
     already meant to be public (frontend, `output/*.json`, `API.md`).
     Caddy redirects it to HTTPS rather than serving over it directly.
   - **HTTPS (443)**: source `0.0.0.0/0` — same reasoning as port 80.
6. Storage: default 8-30GB gp3 is plenty.
7. Launch, then note the instance's **public IPv4 address**.

## 2. One-time server setup

SSH in (`ssh -i your-key.pem ubuntu@<EC2_PUBLIC_IP>`), then:

```bash
# Install Docker (the convenience script includes the Compose plugin)
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
newgrp docker    # or log out and back in

docker compose version    # confirm the Compose plugin is present

# Somewhere to persist scraped data across deploys (Postgres also
# mirrors everything if DATABASE_URL is set, but this keeps the JSON
# files — what the frontend actually reads — surviving a redeploy too)
mkdir -p ~/data_scrapper_output
```

Create `~/data_scrapper.env` — **outside** the `~/data_scrapper/` repo
directory (rsync's `--delete` would otherwise wipe it on the next sync).
This is a real production copy of your local `.env`, plus two additions:

```bash
nano ~/data_scrapper.env
```

```env
APIFY_TOKEN=...
DATABASE_URL=postgresql://...
ANTHROPIC_API_KEY=...
GRAPH_CLIENT_ID=...
GRAPH_TENANT_ID=common
SEC_USER_AGENT=...

# New for this deployment — generate a long random string, e.g.:
#   python -c "import secrets; print(secrets.token_urlsafe(32))"
API_KEY=<a long random secret you generate>

SERVE_HOST=0.0.0.0
PORT=8080
```

**Never commit this file or paste its contents into a GitHub secret** —
it stays only on this instance and in your local `.env`.

## 3. Add GitHub repo secrets

Repo → Settings → Secrets and variables → Actions → New repository secret:

| Secret | Value |
|---|---|
| `EC2_HOST` | the instance's public IP |
| `EC2_USER` | `ubuntu` |
| `EC2_SSH_KEY` | the full contents of the `.pem` file from step 1 |

That's the complete secret list — no Docker registry credentials needed.

## 4. Push

```bash
git remote add origin <your-repo-url>
git push -u origin main
```

The `deploy.yml` workflow runs on every push to `main` (or manually via
Actions → Deploy to EC2 → Run workflow). First run: check the Actions tab
for the build/deploy log.

## 5. Verify

- `https://<EC2_PUBLIC_IP>/frontend/` should load the app, after clicking
  through the self-signed cert warning once (see "HTTPS" above).
- `curl -k https://<EC2_PUBLIC_IP>/api/accounts` should 401 without a key
  (`-k` skips cert verification, since it's self-signed), and return data
  with `-H "X-API-Key: <your API_KEY>"` added.
- In the frontend itself, paste the same `API_KEY` into the small field
  next to the theme toggle (top right) — it's saved in that browser's
  `localStorage` and sent automatically on Run Pipeline / Save as Target
  / Send Mail from then on.

## Updating later

Just push to `main` — the pipeline rebuilds and restarts both containers.
`output/` and Postgres data both survive (the volume mount and the
database are untouched by a redeploy); so does the Caddy TLS certificate
(the `caddy_data` volume persists across redeploys too).

## Rolling back / troubleshooting

```bash
ssh -i your-key.pem ubuntu@<EC2_PUBLIC_IP>
cd ~/data_scrapper
docker compose logs app            # see what the Python app printed
docker compose logs caddy          # see what Caddy printed (TLS issues, etc.)
docker compose ps                  # confirm both are running
```

The box itself has no git history — code arrives via `rsync`, not a
clone — so to roll back to an earlier commit, go to the GitHub repo's
**Actions** tab, find that commit's successful "Deploy to EC2" run, and
use **Re-run all jobs**. That re-syncs the repo at that commit and
rebuilds.
