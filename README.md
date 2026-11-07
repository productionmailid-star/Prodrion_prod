# Prodrion (ProdMind)

Turn customer reviews into shipped fixes. Prodrion connects to the platforms
where a company's users leave feedback, isolates the negative reviews, groups
similar complaints into buckets, then reads the company's GitHub repository to
work out where the code would need to change — and opens a ticket containing an
AI-generated patch the team can apply or rewrite.

Billing is **credit-based**: fetching reviews, bucketing issues, and generating
code each consume credits from a server-held ledger.

> Status: prototype being hardened for production. See
> [`docs/production-readiness.md`](docs/production-readiness.md) for the
> assessment and the milestone plan.

---

## The pipeline

```
1. Connect        User connects review sources (App Store, Play Store, Zendesk,
                  Slack) on the dashboard's Integrations page, and installs
                  the GitHub App on their repo.
                  -> web/dashboard.html (hooks table) + github_app.py

2. Fetch          Each connected source is polled on its own schedule.
                  -> feedback_platform.py poll_hook() + worker.py's
                     self-rescheduling "poll_tick" job

3. Filter         classify_feedback() tags every item's sentiment in the
                  same LLM call that assigns taxonomy labels; only
                  "negative" feedback is clustered into a suggested fix.
                  -> feedback_platform.py classify_feedback()

4. Bucket         Negative feedback is grouped by label; buckets at/above a
                  size threshold are surfaced on the dashboard as a
                  suggested fix. Duplicates of existing tickets are linked
                  instead of re-created.
                  -> feedback_platform.py rebuild_buckets() / match_to_jira()
                  -> worker.py's "rebuild_buckets_tick" job
                  -> supabase_sync.py (writes suggested_features)

5. Generate fix   The user clicks "Generate fix" on a bucket. The repo is
                  scanned to find the files/lines implementing the affected
                  behaviour, and an AI-generated change is drafted for them.
                  -> POST /me/repo-analysis -> worker.py's "analyze_repo" job
                  -> repo-agent/  (scan -> chunk -> index -> discover -> locate -> plan)

6. Ticket         A GitHub issue is opened with the suggested patch. The
                  user applies it as-is, edits it, or ignores it.
                  -> github_app.py create_issue()
```

## Components

| Path | Responsibility |
| --- | --- |
| `feedback_platform.py` | Ingestion + classification + bucketing engine. FastAPI service, SQLite store, revenue/velocity reporting, natural-language query. |
| `supabase_sync.py` | Server-only bridge to the dashboard's Supabase database: reads per-workspace `hooks`/`sync_settings`, writes `suggested_features`/`activity_log`/`dashboard_stats`/billing usage. |
| `jobs.py` / `worker.py` | Durable job queue + the worker process. Self-rescheduling "*_tick" jobs (poll_tick, rebuild_buckets_tick, jira_sync_tick, ...) replace the old in-process scheduler. |
| `repo-agent/` | Local code-analysis pipeline: scans a repository, indexes it, and produces line-specific change suggestions for a goal. Read-only — never writes to the target repo. |
| `repo_agent_core.py` | Synchronous `analyze(repo_path, goal)` wrapper around the `repo-agent/` pipeline, used by the worker. |
| `repo_agent_server.py` | FastAPI wrapper exposing the pipeline over HTTP + SSE, for manual/interactive use — not on the production path. |
| `github_app.py` | GitHub App auth (installation JWT + token cache), sandboxed shallow clone of a user repo, and issue creation. |
| `credits.py` | Server-authoritative credit ledger — billable actions charge before work is queued. |
| `web/` | Frontend: landing page, auth, and the workspace dashboard. Backed by Supabase (auth + application database); pipeline results arrive via `supabase_sync.py`. |
| `injection.py` | Dev scratch — pushes GitHub issues into the feedback webhook for local testing. Not part of the product. |

## Deployment

One Ubuntu VPS (Hostinger KVM 4) runs the API (`prodrion-api`), the background
worker (`prodrion-worker`), SQLite, and the on-disk vector indexes; nginx
terminates TLS and serves the static dashboard. LLM inference is the Gemini
free-tier API. See [`docs/deployment.md`](docs/deployment.md) and the
[`deploy/`](deploy/) directory (systemd units, nginx config, `deploy.sh`).

## Processes

| Process | Command | Responsibility |
| --- | --- | --- |
| API | `python feedback_platform.py` | HTTP endpoints; webhooks enqueue jobs and return `202` |
| Worker | `python worker.py` | Drains the job queue (per-feedback pipeline) and runs the APScheduler polling / drift / velocity jobs |

Both share the SQLite database and use it as the job queue — no external broker.
For local development set `PRODRION_INLINE_WORKER=true` to run the worker loop
inside the API process instead.

## Local development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env
PRODRION_INLINE_WORKER=true python feedback_platform.py   # API + worker in one process
```

Run the tests:

```bash
pytest
```
