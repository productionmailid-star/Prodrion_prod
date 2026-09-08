# Feedback Intelligence Platform

**100% free and open-source.** No paid APIs required.

---

## Open-Source Replacements for Paid Services

| Paid Service | Free Replacement Used |
|---|---|
| GPT-4 | Ollama (local LLM — llama3.2, mistral, etc.) |
| Pinecone | FAISS (in-process vector index) |
| Managed Postgres | SQLite (zero-config, file-based) |
| Cron / Temporal | APScheduler (in-process scheduler) |
| Datadog / Mixpanel | Custom velocity watcher → free Slack alerts |

---

## Setup

### 1. Install Ollama (local LLM — free forever)
```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull llama3.2   # or mistral, phi3, etc.
```

### 2. Install Python dependencies
```bash
pip install -r requirements.txt
```

### 3. Configure environment variables
```bash
# Required: none (runs fully on defaults with demo data)

# Optional — add what you have:
export OLLAMA_MODEL="llama3.2:latest"         # or mistral, phi3, etc.

export SLACK_BOT_TOKEN="xoxb-..."             # free Slack app
export SLACK_ALERT_CHANNEL="#alerts"

export ZENDESK_URL="https://yourco.zendesk.com"
export ZENDESK_EMAIL="you@yourco.com"
export ZENDESK_TOKEN="your_token"

export APP_STORE_APP_ID="1234567890"
export PLAY_STORE_APP_ID="com.example.app"

export JIRA_URL="https://yourco.atlassian.net"
export JIRA_EMAIL="you@yourco.com"
export JIRA_TOKEN="your_token"
export JIRA_PROJECT="PROJ"

export HUBSPOT_API_KEY="your_key"             # HubSpot free tier
```

### 4. Run
```bash
python feedback_platform.py
```

---

## API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| POST | `/webhook/feedback` | Ingest any JSON feedback |
| POST | `/webhook/slack` | Slack Events API receiver |
| GET | `/query?q=...` | Natural language → SQL query |
| GET | `/revenue-impact` | Issues ranked by ARR impact |
| POST | `/bootstrap-taxonomy?url=...` | Auto-generate taxonomy from docs URL |
| GET | `/taxonomy` | View current taxonomy |
| POST | `/import-users?csv_path=...` | Import users + ARR from CSV |
| GET | `/health` | System status |

---

## Example Usage

### Ingest feedback via webhook
```bash
curl -X POST http://localhost:8000/webhook/feedback \
  -H "Content-Type: application/json" \
  -d '{
    "text": "I cannot log in via SSO — it keeps timing out",
    "email": "john@bigcorp.com",
    "source": "typeform",
    "timestamp": "2024-01-15T10:30:00"
  }'
```

### Natural language query
```bash
curl "http://localhost:8000/query?q=Show+me+all+login+issues+from+Enterprise+customers+last+week"
```

### Bootstrap taxonomy from your docs
```bash
curl -X POST "http://localhost:8000/bootstrap-taxonomy?url=https://yourproduct.com/docs"
```

### Revenue impact dashboard
```bash
curl http://localhost:8000/revenue-impact
# Returns: "Fixing 'Authentication > Login > SSO' is worth $142,000/year across 18 users."
```

### Import users with ARR from CSV
Your CSV should have these columns: `email, user_id, arr, plan_type, company`
```bash
curl -X POST "http://localhost:8000/import-users?csv_path=/path/to/users.csv"
```

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│              ENGINE 1: INGESTION  (The "Hook")           │
│  Polling (APScheduler) → Zendesk, App Store, Play Store │
│  Webhook (FastAPI)     → Typeform, Slack, custom         │
│  Normalizer            → StandardFeedbackObject          │
└──────────────────────────┬──────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────┐
│              ENGINE 2: TAXONOMY  (The "Brain")           │
│  Bootstrapper  → Scrape docs → Ollama → 3-level tree    │
│  Classifier    → Ollama multi-label classification       │
│  Drift Detector→ HDBSCAN clustering → suggest new nodes  │
└──────────────────────────┬──────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────┐
│              ENGINE 3: REVENUE  (The "Ranker")           │
│  Identity Resolver → match email → users table           │
│  CRM Sync          → HubSpot free tier or CSV import     │
│  Aggregator        → Issue_Score = COUNT × SUM(ARR)      │
└──────────────────────────┬──────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────┐
│              ENGINE 4: DEDUPLICATION  (The "Loop")       │
│  Jira/Linear Sync  → pull In Progress + Backlog tickets  │
│  FAISS VectorStore → embed + index tickets               │
│  Semantic Matcher  → cosine sim ≥ 0.85 → auto-link      │
└──────────────────────────┬──────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────┐
│              ENGINE 5: DASHBOARD  (The "UI")             │
│  Text-to-SQL  → Ollama generates SQL → SQLite runs it   │
│  Trend Watcher→ week-over-week velocity → Slack alerts   │
└─────────────────────────────────────────────────────────┘
```
