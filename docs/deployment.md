# Deployment — Hostinger KVM 4 (Ubuntu)

One VPS runs everything: the API (`prodrion-api`), the background worker
(`prodrion-worker`), SQLite, and the on-disk FAISS/Chroma indexes. nginx
terminates TLS and serves the static dashboard. LLM inference is the Gemini
free-tier API.

```
Browser ──▶ nginx :443
             ├─ app.example.com  ──▶  /opt/prodrion/web        (static)
             └─ api.example.com  ──▶  127.0.0.1:8000  (prodrion-api, 2 workers)
                                            │  jobs table
                                            ▼
                                     prodrion-worker  ──▶  Gemini API
```

## 1. Provision

- Ubuntu 22.04/24.04, KVM 4 (~4 vCPU / 16 GB / 200 GB NVMe).
- DNS: `app.example.com` and `api.example.com` → the VPS IP.

```bash
sudo apt update && sudo apt install -y python3-venv python3-full git nginx certbot python3-certbot-nginx ufw nodejs npm
sudo ufw allow OpenSSH && sudo ufw allow 'Nginx Full' && sudo ufw enable
sudo adduser --system --group --home /opt/prodrion prodrion
```

(`nodejs`/`npm` build the dashboard's CSS — see step 6 and `web/BUILD.md`.)

## 2. Code + venv

```bash
sudo -u prodrion git clone <REPO_URL> /opt/prodrion
cd /opt/prodrion
sudo -u prodrion git checkout production-hardening
sudo -u prodrion python3 -m venv .venv
sudo -u prodrion .venv/bin/pip install -U pip
sudo -u prodrion .venv/bin/pip install -r requirements.txt
sudo -u prodrion mkdir -p /opt/prodrion/var
```

## 3. Configuration

```bash
sudo mkdir -p /etc/prodrion
sudo cp /opt/prodrion/deploy/prodrion.env.example /etc/prodrion/prodrion.env
sudo chmod 600 /etc/prodrion/prodrion.env
sudo chown prodrion:prodrion /etc/prodrion/prodrion.env
sudo nano /etc/prodrion/prodrion.env
```

`deploy/prodrion.env.example` is a systemd-clean template (no inline comments).
Fill in every `CHANGE_ME`:

- `PRODRION_API_AUTH_TOKEN`, `PRODRION_WEBHOOK_SECRET` — `openssl rand -hex 32`
- `PRODRION_GEMINI_API_KEY` — from aistudio.google.com
- `PRODRION_SUPABASE_JWT_SECRET` — Supabase project → Settings → API → JWT Secret
- `PRODRION_SUPABASE_DB_URL` — Supabase project → Settings → Database →
  Connection string → URI. Required in production; lets the backend read
  per-workspace `hooks`/`sync_settings` and write `suggested_features`/
  `activity_log`/`dashboard_stats`/billing usage (see `supabase_sync.py`).

The API refuses to start in production if a required value is missing or the
config is incoherent (see `config.py` `_validate_production_coherence`). The
full annotated variable list is `.env.example`.

## 4. systemd

```bash
sudo cp /opt/prodrion/deploy/prodrion-api.service /etc/systemd/system/
sudo cp /opt/prodrion/deploy/prodrion-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now prodrion-api prodrion-worker
sudo -u prodrion .venv/bin/python -c "from db import init_db; init_db()"
journalctl -u prodrion-api -u prodrion-worker -f
```

## 5. nginx + TLS

```bash
sudo cp /opt/prodrion/deploy/nginx.conf /etc/nginx/sites-available/prodrion
# edit server_name / paths to your domains, then:
sudo ln -s /etc/nginx/sites-available/prodrion /etc/nginx/sites-enabled/
sudo certbot --nginx -d app.example.com -d api.example.com
sudo nginx -t && sudo systemctl reload nginx
```

## 6. Frontend

Point the dashboard at the API and (optionally) the GitHub App install page:

```bash
# in web/dashboard.html
const API_BASE = 'https://api.example.com';
const GITHUB_APP_INSTALL_URL = 'https://github.com/apps/your-app/installations/new';
```

Build the CSS (see `web/BUILD.md` — `deploy.sh` also does this on every deploy):

```bash
cd /opt/prodrion/web && npm install && npm run build:css
```

Run `web/migrations/001_billing_stats_readonly.sql` and
`web/migrations/002_github_installation.sql`, in that order, in the Supabase
SQL editor.

## Operations

| Task | Command |
| --- | --- |
| Deploy / upgrade | `/opt/prodrion/deploy/deploy.sh production-hardening` |
| Logs | `journalctl -u prodrion-api -u prodrion-worker -f` |
| Health | `curl -s https://api.example.com/health` |
| Dead-letter jobs | `curl -s -H "Authorization: Bearer $TOKEN" https://api.example.com/jobs?status=dead` |
| Restart | `sudo systemctl restart prodrion-api prodrion-worker` |
| Rollback | `deploy.sh <previous-git-sha>` |

### Backups

SQLite is the whole datastore. Nightly online backup:

```bash
# /etc/cron.d/prodrion-backup
0 3 * * * prodrion sqlite3 /opt/prodrion/var/prodrion.db ".backup '/opt/prodrion/var/backup-$(date +\%F).db'" && find /opt/prodrion/var -name 'backup-*.db' -mtime +14 -delete
```

Copy backups off-box (rclone / scp) as well.

### Notes / limitations

- SQLite + WAL is fine at MVP volume; move to Postgres on the same box if write
  contention shows up in the logs (`database is locked`).
- `repo_agent_server.py` (SSE, port 7845) is **not** exposed by nginx — the
  worker calls `repo_agent_core` in-process. Run it manually only for debugging.
- Tailwind CSS is pre-built (`web/dist/app.css`, not committed — `deploy.sh`
  builds it); the jsDelivr-hosted Supabase SDK is still an external
  dependency (pinned with SRI) — vendor it into `web/` to remove that too.
- No periodic polling/sync runs until `prodrion-worker` has started at least
  once with `PRODRION_SUPABASE_DB_URL` configured (it seeds every "*_tick"
  job chain on startup — see `worker.seed_periodic_jobs`).
