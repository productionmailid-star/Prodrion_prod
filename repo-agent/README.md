# Repo fix agent

Given a problem description (and, in production, the customer reports behind
it), the agent investigates a repository, makes the change, verifies it, and
returns a unified diff. It replaces the earlier fixed pipeline
(scan → index → locate → plan), which could only suggest one replacement line
per file and never checked its output.

## How it works

```
problem + customer reports + repository map
        │
        ▼
┌─► model replies with tool calls (JSON) ──────────────┐
│     list_files · search · read_file                  │  explore
│     edit_file · write_file · delete_file             │  change (any number of files)
│     run · run_tests            (sandbox only)        │  reproduce / check
│                                                      │
└── tool results go back to the model ◄────────────────┘
        │  finish
        ▼
verification gate ──fails──► failures returned to the model, loop continues
        │ passes
        ▼
unified diff (git apply format) + root cause + verification report
```

The gate accepts a fix only if:

1. at least one file actually changed;
2. every changed file still parses (Python, JSON, TOML via the standard
   library; JS/TS/TSX/Java/Go/Rust/C/C++/… via tree-sitter). A file that was
   already broken before the change is not held against it;
3. when a sandbox is enabled, the repository's own tests have not regressed
   against a baseline recorded before the first edit.

If the agent gives up, runs out of steps or time, or cannot get past the gate,
there is no result: the worker opens no ticket and refunds the credits.

## Layout

```
repo-agent/
  main.py                 CLI
  agents/
    fix_agent.py          the loop, prompts, reply parsing, verification gate
    tools.py              the tools and what the model sees back from them
  repo/
    workspace.py          path confinement, change tracking, unified diff
    repomap.py            file + symbol map given to the model up front
    verify.py             syntax checks
    sandbox.py            docker / local execution, test detection, baseline
    filters.py            ignored directories
```

`repo_agent_core.analyze()` (repo root) is the entry point used by the worker,
the SSE server and the CLI.

## Usage

```bash
python repo-agent/main.py /path/to/repo "Checkout crashes when no coupon is applied" \
    --report "paid without a promo code and the app died" --out fix.patch
```

The repository you point at is never modified; the agent works on a temporary
copy. Apply the result with `git apply fix.patch`.

## Configuration

All settings are `PRODRION_REPO_AGENT_*` (see `.env.example`). The important
one is the sandbox:

| `PRODRION_REPO_AGENT_SANDBOX` | Effect |
| --- | --- |
| `off` (default) | Nothing from the target repo is executed. Fixes are syntax-checked only, and the ticket says tests were not run. |
| `docker` | Tests and the agent's `run` commands execute in a throwaway container: no network (except while installing dependencies), no capabilities, non-root, read-only root filesystem, CPU / memory / PID limits, none of the worker's environment. |
| `local` | Runs on this machine with a scrubbed environment. Development only; refused when `PRODRION_ENVIRONMENT=production`. |

Test commands are auto-detected (`pytest` when Python tests exist, `npm test`
when `package.json` defines one) and can be overridden with
`PRODRION_REPO_AGENT_SETUP_COMMAND` / `PRODRION_REPO_AGENT_TEST_COMMAND`.

## Safety properties

- Every path the model names is resolved inside the workspace; `..`, absolute
  paths, ignored directories (`.git`, `node_modules`, …) and symlinks that
  point outside the repository are refused.
- Customer reports and file contents are passed to the model as quoted data,
  and the system prompt tells it not to follow instructions inside them. The
  output is always a ticket for a person to review — nothing is pushed or
  merged.
- The GitHub installation token is scrubbed from the clone before the agent
  (or any test) can read it.

## Limits

- **Quality depends on the model.** The loop, tools and gate are tested with a
  scripted model (`tests/test_fix_agent.py`). Live check (2026-11-16, GLM-5.3
  via `PRODRION_LLM_PROVIDER=zai`, sandbox off, 8 seeded bugs in a ~43 KB
  Python + JS shop app, scored by hidden tests the agent never saw): 8/8 with
  the repo inlined (~$0.01 and 1–2 requests per case) and 8/8 when forced to
  explore with search/read (3–5 requests, ~$0.015 per case), including
  correctly refusing a "prices too high" complaint. That repo is small and the
  bugs are single-cause.

  Harder check, same day: 10 tasks drawn at random (fixed seed, none
  rejected) from SWE-bench Verified's Django 4.0–5.0 and SymPy 1.7–1.12
  tasks — real GitHub issues in large repos (Django: 5,700 files), scored by
  the project's own hidden tests. With GLM-5.3 at `reasoning_effort=low` and
  the sandbox off: **7/10 solved, $1.18 total** (median ~$0.05 and ~1 minute
  per task). Of the three misses, two ran into the $0.35 per-task cap while
  still exploring, and one changed a different file than the real fix. At
  `reasoning_effort=high` the same model was ~4x more expensive per task and
  hit a $0.50 cap on 4 of 7. Ten tasks is a small sample (roughly ±15
  points), so treat this as "in the right range", not a precise score.
- **With the sandbox off, "verified" means "parses".** A change can parse and
  still be wrong (a bad import, a logic error). Enable the Docker sandbox to
  have tests gate the fix; even then, the fix is only as well-checked as the
  repository's tests are.
- **Quota.** One job can use up to `PRODRION_REPO_AGENT_MAX_STEPS` model
  requests (default 40). As of 2026-11 the Gemini free tier allows only **20
  generate requests per model per day** (e.g. `gemini-3.8-flash`), so a single
  hard job can exhaust a day's quota. To stretch it, repositories under ~60 KB
  of text are shown to the model in full up front (no read requests needed),
  and a reply may make its final edits and `finish` together. When the
  provider reports the daily quota is used up, the agent stops immediately
  (no retries burned) and the worker postpones the job until the quota resets
  rather than failing it. A paid key is needed for real volume.
- **Vague complaints.** A bucket like "app is slow" may not contain enough to
  locate a defect; the agent is told to give up rather than guess.
- The Docker sandbox has unit tests for its command line and for the
  baseline/regression logic, but has not been exercised against a real Docker
  daemon in this repository's test suite.
