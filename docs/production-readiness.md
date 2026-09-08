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
> docs/credits-and-billing.md (run manually). 1.2, 1.3, 1.7-1.9 outstanding.

| ID | Issue | Fix type |
| --- | --- | --- |
| 1.1 | **Synchronous LLM work in request handlers.** `POST /webhook/feedback` runs the full pipeline inline; no queue, retry, or dead-letter. | architectural |
| 1.2 | **SQLite under concurrent threads** with no WAL/busy-timeout, and **no idempotency key** — re-polled sources create duplicate rows that corrupt every downstream count. | quick fix (WAL + UNIQUE key); Postgres later |
| 1.3 | **Two disconnected halves** (`web/` vs `feedback_platform.py`). Resolved: they converge into one product. | architectural |
| 1.4 | **Client-controlled billing/usage.** The dashboard writes `billing`/`dashboard_stats` directly from the browser under owner-write RLS. With credit-based pricing this must be server-authoritative. | architectural |
| 1.5 | **Error handling swallows everything.** Broad `except Exception` returning `""`/`None`/`[]`; failures look like empty data. | quick fix |
| 1.6 | **No structured logging, request IDs, metrics, or real health checks.** | quick fix → infra |
| 1.7 | **Zero tests, no CI.** | architectural (process) |
| 1.8 | **Dependencies unpinned/unaudited.** `>=` bounds only, no lockfile; `sqlalchemy` unused; `reviewer.py` deps missing; fragile store scrapers. | quick fix |
| 1.9 | **GitHub App + user-repo access** is a new surface: token storage, sandboxed clone of untrusted repo content, PR/ticket creation, rate limits. | architectural |

## P2 — Medium

> Status: 2.1, 2.3, 2.4, 2.10 addressed in milestone 4; 2.2 partially (Zendesk
> requester/subject fallbacks, stable external ids per source — full contract
> tests against recorded API responses still pending). 2.5–2.9 outstanding.

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
| 9 | Deploy | systemd units, nginx + TLS, deploy script, runbook. |
