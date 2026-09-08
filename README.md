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
1. Connect        User links review sources (App Store, Play Store, Zendesk,
                  Slack, Reddit, …) and installs the GitHub App on their repo.

2. Fetch          Reviews about the user's company are pulled from each source.
                  -> reviewer.py  (fetchers + sentiment filter)

3. Filter         Only negative reviews are kept.
                  -> reviewer.py  (DistilBERT primary, VADER fallback)

4. Bucket         Similar issues are embedded and grouped; duplicates of
                  existing tickets are linked instead of re-created.
                  -> feedback_platform.py  (embeddings + clustering + FAISS)

5. Locate         For a bucket, the user's repo is scanned to find the files
                  and lines that implement the affected behaviour.
                  -> repo-agent/  (scan -> chunk -> index -> discover -> locate)

6. Draft          An AI-generated code change is produced for those lines.
                  -> repo-agent/agents/planner.py

7. Ticket         A ticket is created for the user with the suggested patch.
                  The user applies it as-is, edits it, or ignores it.
```

## Components

| Path | Responsibility |
| --- | --- |
| `feedback_platform.py` | Ingestion + bucketing engine. FastAPI service, SQLite store, scheduled pollers, revenue/velocity reporting, natural-language query. |
| `reviewer.py` | Multi-platform review fetching (Google Play, App Store, Reddit) and negative-review filtering. |
| `repo-agent/` | Local code-analysis pipeline: scans a repository, indexes it, and produces line-specific change suggestions for a goal. Read-only — never writes to the target repo. |
| `repo_agent_server.py` | FastAPI wrapper that exposes the `repo-agent/` pipeline over HTTP + SSE. |
| `goal_to_repo_agent.py` | Connector: turns bucketed feedback labels into goals and runs them through `repo_agent_server.py`. |
| `web/` | Frontend: landing page, auth, and the workspace dashboard. Backed by Supabase (auth + application database). |
| `injection.py` | Dev scratch — pushes GitHub issues into the feedback webhook for local testing. Not part of the product. |

## Deployment

Backend and AI orchestration run on a **single Ubuntu VM** (Azure, funded by the
GitHub Student Pack credit). LLM inference is delegated to a **hosted API**;
the VM runs the FastAPI service, a background worker, the SQLite/Postgres store,
and the on-disk vector index. The frontend is served as static files and talks
to Supabase directly for auth and application data.

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
