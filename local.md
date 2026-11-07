# Running Prodrion entirely on your laptop (Ollama, no cloud)

Everything runs locally: the API, the background worker, SQLite, the FAISS
index, and the LLM (via Ollama). No Gemini key, no Hostinger, no Supabase
needed for the backend. Supabase is only needed if you also want the `web/`
dashboard UI (see the optional section at the end).

---

## 0. Prerequisites

- **Python 3.10+**
- **Ollama** — <https://ollama.com/download>
- **git** (you already have the repo)
- ~6–8 GB free RAM while a model is loaded (llama3.2 3B ≈ 3–4 GB)

---

## 1. Install and start Ollama

```bash
# after installing Ollama, it runs a server on http://localhost:11434
ollama pull llama3.2            # text generation (classification, text-to-SQL, taxonomy)
ollama pull nomic-embed-text   # embeddings (feedback + ticket vectors)

# verify
ollama list
curl http://localhost:11434/api/tags
```

Leave Ollama running.

---

## 2. Python environment

From the repo root:

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install --upgrade pip
pip install -r requirements.txt
```

> On Debian/Ubuntu you may see `externally-managed-environment` — that's why we
> use a venv. Always activate `.venv` before running anything.

The first install pulls a fair amount (faiss, chromadb, tree-sitter, etc.).
`google-genai` is installed too but is never imported when the provider is
`ollama`.

---

## 3. Configuration

```bash
cp .env.example .env
```

Edit `.env` and set the LLM provider to Ollama (leave everything else at
defaults):

```
PRODRION_ENVIRONMENT=development
PRODRION_LLM_PROVIDER=ollama
PRODRION_OLLAMA_URL=http://localhost:11434/api/generate
PRODRION_OLLAMA_MODEL=llama3.2
PRODRION_OLLAMA_EMBED_MODEL=nomic-embed-text
```

Optional, but useful for a first run:

```
# gives match_to_jira something to link against (4 demo tickets)
PRODRION_LOAD_DEMO_DATA=true

# build a real taxonomy from a product's docs instead of "Uncategorized"
PRODRION_TAXONOMY_BOOTSTRAP_URL=https://plausible.io/docs
```

You can leave these **unset** for local use (dev mode logs a one-time warning
and allows the request):

- `PRODRION_API_AUTH_TOKEN` — admin endpoints are open locally
- `PRODRION_WEBHOOK_SECRET` — `/webhook/feedback` needs no header locally
- `PRODRION_SUPABASE_JWT_SECRET` — `/me/credits` returns a `dev-user`
- `PRODRION_CREDITS_ENABLED` — stays `false`, so nothing is charged

> The `.env` file is read from the directory you start the process in, so run
> everything from the repo root.

---

## 4. Run the backend

**One process (simplest):**

```bash
PRODRION_INLINE_WORKER=true python feedback_platform.py
```

This starts the API on `http://127.0.0.1:8000` and runs the worker loop +
scheduler in the same process.

**Two processes (closer to production):**

```bash
# terminal 1
python feedback_platform.py

# terminal 2
python worker.py
```

On first start it creates `feedback_platform.db` in the repo root.

---

## 5. Verify it works

```bash
# health
curl -s http://127.0.0.1:8000/health

# ingest one piece of feedback -> returns {"status":"queued","job_id":"..."}
curl -s -X POST http://127.0.0.1:8000/webhook/feedback \
  -H 'Content-Type: application/json' \
  -d '{"text":"I cannot log in via SSO, it keeps timing out","email":"jane@bigcorp.com","source":"manual"}'

# watch the job get processed (pending -> running -> done)
curl -s "http://127.0.0.1:8000/jobs?limit=5"

# once done, query the data (LLM turns this into SQL)
curl -s "http://127.0.0.1:8000/query?q=show%20all%20feedback"

# issues ranked by revenue impact
curl -s http://127.0.0.1:8000/revenue-impact

# current taxonomy
curl -s http://127.0.0.1:8000/taxonomy
```

The **worker** does the LLM work. If a job stays `pending`, the worker isn't
running. If jobs go to `dead`, inspect them:

```bash
curl -s "http://127.0.0.1:8000/jobs?status=dead"
```

### Load a batch of sample data (optional)

`injection.py` pulls open GitHub issues from `plausible/analytics` and posts
them to the webhook:

```bash
python injection.py
```

### Attach ARR to feedback (optional)

`resolve_user_revenue` matches feedback `email` against a `users` table. Import
one:

```bash
printf 'email,user_id,arr,plan_type,company\njane@bigcorp.com,u1,50000,enterprise,BigCorp\n' \
  | curl -s -X POST http://127.0.0.1:8000/import-users \
    -H 'Content-Type: text/csv' --data-binary @-
```

Re-ingest that user's feedback afterwards so the ARR is picked up.

---

## 6. Run the tests (optional)

```bash
pip install -r requirements-dev.txt
pytest        # 120 tests, run on a fake provider — Ollama not required
```

---

## 7. The repo-agent (code-change suggestions) — optional

This turns a goal into line-specific patch suggestions for a local codebase.
It uses the same Ollama provider. A code-tuned model helps:

```bash
ollama pull qwen2.5-coder        # or: ollama pull codellama
# set PRODRION_OLLAMA_MODEL=qwen2.5-coder in .env if you want it for codegen
```

**Standalone CLI:**

```bash
cd repo-agent
python main.py
# prompts for a repo path and a goal; creates ./chroma_db on first run
```

**As a service (for goal_to_repo_agent.py):**

```bash
uvicorn repo_agent_server:app --port 7845     # terminal 3
python goal_to_repo_agent.py                   # reads feedback -> goals -> patches
```

The GitHub-issue creation path (`POST /repo-analysis`, the `analyze_repo` job)
needs a GitHub App and is **not** required for local use — analyse a local
path directly instead.

---

## 8. The dashboard UI (`web/`) — optional, needs Supabase

The backend API works without this. To use the actual dashboard:

1. Create a free project at <https://supabase.com>.
2. In the SQL editor, run `web/supabase_schema.sql`, then
   `web/migrations/001_billing_stats_readonly.sql`.
3. In `web/index.html`, `web/dashboard.html`, `web/auth/auth-v2.html` replace
   `SUPABASE_URL` / `SUPABASE_ANON` with **your** project's values.
4. In `web/dashboard.html` set `const API_BASE = 'http://127.0.0.1:8000';`.
5. In `.env` set `PRODRION_CORS_ALLOW_ORIGINS=http://localhost:8080` and
   `PRODRION_SUPABASE_JWT_SECRET=<your project's JWT secret>` (Supabase →
   Settings → API), then restart the backend.
6. Serve the static files:
   ```bash
   cd web && python3 -m http.server 8080
   # open http://localhost:8080
   ```

Tailwind still loads from a CDN in dev; `web/BUILD.md` covers building a static
stylesheet.

---

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| Feedback labels are all `General > Feedback > Uncategorized` | No taxonomy. Set `PRODRION_TAXONOMY_BOOTSTRAP_URL` or `POST /bootstrap-taxonomy?url=<docs URL>`. |
| Jobs stuck at `pending` | Worker not running. Start `python worker.py` or use `PRODRION_INLINE_WORKER=true`. |
| Log line `LLM provider call failed` | Ollama not running / wrong model name. Check `ollama list` and `PRODRION_OLLAMA_MODEL`. |
| `no such file` for `.env` values | Run the process from the repo root (that's where `.env` is read). |
| `jira_ticket` always empty | Expected unless `PRODRION_LOAD_DEMO_DATA=true` or real Jira creds are set. |
| Slow first request | Ollama is loading the model into RAM; subsequent calls are faster. |
| Switched embed model after ingesting data | Old vectors are a different size. Wipe: delete `feedback_platform.db` (and `chroma_db/`) and re-ingest. |

---

## What each piece needs

| Feature | Needs |
| --- | --- |
| Feedback ingest → bucket → query → revenue impact | Python + Ollama only |
| Natural-language `/query` (text-to-SQL) | Ollama |
| Taxonomy auto-build | Ollama + a docs URL |
| Jira dedup / linking | `PRODRION_LOAD_DEMO_DATA=true` or real Jira creds |
| repo-agent patch suggestions | Ollama + `chromadb`/`tree-sitter` (already in requirements) |
| GitHub issue creation | a GitHub App (skip for local) |
| Slack / Zendesk / App Store / Play Store polling | the respective credentials in `.env` |
| Dashboard UI | a Supabase project |
| Credits / billing | `PRODRION_CREDITS_ENABLED=true` (off by default) |
