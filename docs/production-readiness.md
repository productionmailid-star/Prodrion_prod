# Production-readiness assessment & plan

Assessment of the Prodrion prototype and the milestone plan for hardening it.
Priorities are tagged **quick fix** (local change, hours) or **architectural**
(design decision / new infrastructure / multi-file).

Deployment constraints that shape every recommendation:

- Backend + AI orchestration run on **one small Ubuntu VM** (~4 GB RAM,
  ~$100 of Azure credit ≈ 3 months runway).
- LLM inference is delegated to a **hosted API**. The VM does not run local
  models.
- Auth and the application database are **Supabase** (free tier).
- A single node means no replica write-contention, so SQLite + WAL is an
  acceptable MVP store; APScheduler in-process and an on-disk FAISS index are
  fine at this scale.

---

## P0 — Blockers

> Status: 0.1, 0.2, 0.3, 0.5 addressed in milestone 3; 0.4 in milestone 1.
> Follow-ups tracked below (repo-agent path allow-list will tighten further
> once user repos are cloned into a per-workspace sandbox in milestone 7).

| ID | Issue | Fix type |
| --- | --- | --- |
| 0.1 | **No auth on the Python API.** Every endpoint is open; `host=0.0.0.0`. Full read of feedback/emails/ARR and destructive writes to anyone who can reach the port. | architectural (interim: static bearer token + bind localhost) |
| 0.2 | **`GET /query` runs LLM-generated SQL** after only checking it starts with `SELECT`. No server-enforced `LIMIT`, no timeout, no table allow-list. Data exfiltration + DoS + prompt injection via feedback text. | architectural |
| 0.3 | **Arbitrary server-side file read** via `/import-users?csv_path=` and repo-agent `repo_path`. | quick fix → architectural (upload flow) |
| 0.4 | **No secrets story.** No `.env`/`.env.example`, silent empty-string defaults, runtime artifacts committed. | quick fix |
| 0.5 | **Webhooks unauthenticated/unverified.** No Slack signature check; unbounded request body. | quick fix |

## P1 — High

> Status: 1.1 addressed in milestone 5 (DB-backed job queue + `worker.py`).
> 1.5 + 1.6 addressed in milestone 6: `observability.py` (JSON/text logging with
> `PRODRION_LOG_LEVEL`/`LOG_FORMAT`, `X-Request-ID` propagation, consistent JSON
> error bodies that don't leak internals, optional Sentry); `except` blocks now
> log with tracebacks instead of swallowing; `/health` checks the DB and reports
> queue depth.
> 1.4 addressed in milestone 7a: server-authoritative `credits.py` ledger,
> billable actions charge before enqueue, `/webhook/feedback` returns 402 when
> out of credits; Supabase `billing`/`dashboard_stats` lockdown SQL in
> docs/credits-and-billing.md (run manually).
> 1.9 addressed in milestone 7b: `github_app.py` (app JWT, in-memory
> installation-token cache, sandboxed shallow clone with a size cap,
> issue creation, webhook-signature verify) + an `analyze_repo` job type that
> clones -> runs the repo-agent -> opens a GitHub issue with the patch.
> 1.3 addressed in milestones 10-14 (see the milestone plan below): the
> backend and the dashboard now share a `workspace_id`, and
> `supabase_sync.py` is the real bridge between them — milestone 8's
> "Resolved" note for this item was premature; the two halves remained
> unsynced until now. 1.2, 1.7, 1.8 outstanding.

| ID | Issue | Fix type |
| --- | --- | --- |
| 1.1 | **Synchronous LLM work in request handlers.** `POST /webhook/feedback` runs the full pipeline inline; no queue, retry, or dead-letter. | architectural |
| 1.2 | **SQLite under concurrent threads** with no WAL/busy-timeout, and **no idempotency key** — re-polled sources create duplicate rows that corrupt every downstream count. | quick fix (WAL + UNIQUE key); Postgres later |
| 1.3 | **Two disconnected halves** (`web/` vs `feedback_platform.py`). Resolved in milestones 10-14: shared `workspace_id`, `supabase_sync.py` bridge, dashboard "Generate fix" wired to the real pipeline. | architectural |
| 1.4 | **Client-controlled billing/usage.** The dashboard writes `billing`/`dashboard_stats` directly from the browser under owner-write RLS. With credit-based pricing this must be server-authoritative. | architectural |
| 1.5 | **Error handling swallows everything.** Broad `except Exception` returning `""`/`None`/`[]`; failures look like empty data. | quick fix |
| 1.6 | **No structured logging, request IDs, metrics, or real health checks.** | quick fix → infra |
| 1.7 | **Zero tests, no CI.** | architectural (process) |
| 1.8 | **Dependencies unpinned/unaudited.** `>=` bounds only, no lockfile; `sqlalchemy` unused; fragile store scrapers. `reviewer.py` (whose deps were missing) is deleted as of milestone 13 — superseded by the in-pipeline sentiment field. Still outstanding: a real lockfile. | quick fix |
| 1.9 | **GitHub App + user-repo access** is a new surface: token storage, sandboxed clone of untrusted repo content, PR/ticket creation, rate limits. | architectural |

## P2 — Medium

> Status: 2.1, 2.3, 2.4, 2.10 addressed in milestone 4; 2.2 partially.
> 2.7 (CORS) addressed in milestone 8a. 2.8 + 2.9 addressed in milestone 8b:
> Supabase seed rows start at zero (no fabricated stats), `billing`/
> `dashboard_stats` RLS lockdown SQL in web/migrations/001, Supabase JS SDK
> pinned to 2.45.4 with an SRI hash, Tailwind build tooling added
> (web/BUILD.md — the Play CDN line is marked TODO(prod)). 2.5, 2.6 outstanding.

| ID | Issue | Fix type |
| --- | --- | --- |
| 2.1 | Revenue ranking + velocity detection `GROUP BY` the raw `labels` JSON string, so multi-label rows fragment and per-node scoring is wrong. | quick fix |
| 2.2 | Ingestion normalizers don't match the real Zendesk / App Store / HubSpot payloads. | quick fix |
| 2.3 | No poll high-water-mark; fixed "last 1 hour" window overlaps and gaps. | quick fix |
| 2.4 | `datetime.utcnow()` deprecated; mixed tz-awareness in stored timestamps. | quick fix |
| 2.10 | Persisted embeddings are provider/model-specific (dimension + geometry). Switching `PRODRION_LLM_PROVIDER` or the embed model silently invalidates every stored `embedding` BLOB and breaks the FAISS rebuild / drift clustering. Needs an embed-model tag on stored rows + a re-embed path. Milestone 2 only documents it: wipe `feedback.embedding` / `jira_tickets.embedding` and re-ingest after a provider switch. | quick fix → milestone 4 |
| 2.5 | `repo_agent_server` session dict leaks (run starts even if the stream is never opened) and can't survive restart. | quick fix |
| 2.6 | Hardcoded values: `SOURCE_URL_MAP` pinned to `plausible.io`, silent demo-data fallback, ports/host, thresholds. | quick fix |
| 2.7 | CORS unconfigured for the intended browser client. | quick fix |
| 2.8 | Supabase schema nits: unsalted key hash, implicit append-only tables, fabricated seed stats. | quick fix |
| 2.9 | Frontend supply chain: Tailwind Play CDN + floating Supabase SDK, no SRI. | quick fix |

## P3 — Cleanup

Remove committed artifacts; add packaging (`pyproject.toml`), linters and
formatters, type checking; relocate dev scripts; lazy-load the embedding
model; add a
`Dockerfile`/systemd units; SSRF guard on `bootstrap_taxonomy_from_url`.

---

## Milestone plan

| # | Milestone | Scope |
| --- | --- | --- |
| 0 | Repo hygiene + safety net | git init, `.gitignore`, READMEs, `pyproject.toml`, ruff/black, CI, characterization tests for pure functions. No behaviour change. |
| 1 | Config & secrets | `Settings` object, `.env.example`, fail-fast, demo data behind a flag, de-hardcode. |
| 2 | LLM provider abstraction | `llm/` provider interface, hosted client, replace `call_ollama` / `LocalLLM` / `EmbeddingEngine`. |
| 3 | Security P0 | Auth, webhook verification, remove arbitrary paths, fence text-to-SQL, SSRF guard, body-size cap. |
| 4 | Data correctness + durability | WAL + busy-timeout, idempotency key, poll cursor, label-normalization table + fixed aggregation, normalizer fixes, tz sweep. |
| 5 | Async job pipeline | `jobs` table + worker service; webhook enqueues and returns `202`; retry/backoff/dead-letter. |
| 6 | Errors, logging, observability | Narrow exceptions + handlers, JSON logs + request IDs, `/health` checks, optional Sentry. |
| 7 | GitHub App + credit ledger | Install flow, encrypted token storage, sandboxed clone + analysis, PR/ticket creation, atomic credit ledger, RLS lockdown. Own design note first. |
| 8 | Frontend hardening | Build Tailwind, pin Supabase SDK + SRI, seed zeros, wire dashboard to the API, CORS allow-list. |
| 9 | Deploy | systemd units, nginx + TLS, deploy script, runbook. **Done** — `deploy/` + `docs/deployment.md`. |
| 10 | Multi-tenant data model | `workspace_id` on `feedback`/`taxonomy`/`jira_tickets`/`users`/`sync_state`/`buckets` (SQLite); per-workspace FAISS index and taxonomy/credit scoping. Corrects milestone 8's "1.3 Resolved" — the backend and the dashboard were still two unsynced systems with no shared workspace key. |
| 11 | Supabase service-role bridge | `supabase_sync.py`: a direct-Postgres, server-only read of `hooks`/`sync_settings`/`workspaces` and write of `suggested_features`/`activity_log`/`dashboard_stats`/billing usage. `PRODRION_SUPABASE_DB_URL`, required in production. |
| 12 | Dashboard <-> backend wiring | `require_workspace` resolves a signed-in user's real workspace; `GET /me/credits` fixed to use it; new `POST /me/repo-analysis` ("Generate fix") and `POST /me/github/install-callback`. |
| 13 | Sentiment filter + bucket lifecycle | `classify_feedback` now returns sentiment in the same LLM call; only negative feedback feeds `rebuild_buckets()` (new `buckets` table). `reviewer.py` and `goal_to_repo_agent.py` deleted — superseded, were dead/disconnected code. |
| 14 | Per-workspace connectors, no more APScheduler | Zendesk/App Store/Play Store/Slack pollers take credentials from the dashboard's own `hooks` row instead of global env vars. Periodic work (polling, bucket rebuilds, Jira/HubSpot sync, drift/velocity) is now self-rescheduling jobs on the existing durable queue (`jobs.has_active`, `delay_seconds`) instead of an in-process scheduler. |
| 15 | Cleanup | Tailwind build wired into the served pages (Play CDN removed), `repo_agent_server.py` session-dict TTL sweep, dead Zendesk/App-Store/Play-Store/`feedback_api_url`/`repo_agent_url` settings removed. |

Milestones 10-15 intentionally leave real payment collection untouched:
`POST /webhook/stripe` stays the admin-gated stub it was — Stripe integration
is tracked separately and out of scope here.
