"""
=============================================================================
UNIFIED FEEDBACK INTELLIGENCE PLATFORM
=============================================================================
Stack:
  - LLM + embeddings            → pluggable provider (llm.get_llm / get_embedder);
                                  Gemini free-tier API by default, Ollama for local dev
  - FAISS                       → in-process vector index
  - SQLite                      → feedback / taxonomy / ticket store
  - APScheduler                 → in-process polling + drift jobs
  - FastAPI                     → webhook receiver + query API
  - HDBSCAN                     → drift-detection clustering
  - BeautifulSoup4              → taxonomy bootstrapper scraper
  - Slack SDK                   → alerts + ingestion
  - google-play-scraper         → Play Store reviews
  - app-store-scraper           → iOS reviews

Install dependencies:
  pip install -r requirements.txt

Configuration: see config.Settings / .env.example (PRODRION_* variables).

Run:
  python feedback_platform.py
=============================================================================
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import faiss
import numpy as np
import requests
import uvicorn
from apscheduler.schedulers.background import BackgroundScheduler
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Request

from config import settings
from llm import get_embedder, get_llm

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("feedback_platform")

# =============================================================================
# CONFIG
# =============================================================================
# All runtime configuration lives in config.Settings, loaded from the
# environment / .env with the PRODRION_ prefix. See .env.example.


# =============================================================================
# ENGINE 0: DATABASE LAYER
# =============================================================================

DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS feedback (
    id          TEXT PRIMARY KEY,
    text        TEXT NOT NULL,
    user_id     TEXT,
    user_email  TEXT,
    source      TEXT NOT NULL,
    timestamp   TEXT NOT NULL,
    raw_meta    TEXT,
    labels      TEXT,
    arr         REAL DEFAULT 0,
    plan_type   TEXT,
    jira_ticket TEXT,
    jira_status TEXT,
    embedding   BLOB,
    created_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS taxonomy (
    id          TEXT PRIMARY KEY,
    level1      TEXT NOT NULL,
    level2      TEXT,
    level3      TEXT,
    description TEXT,
    created_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS jira_tickets (
    id          TEXT PRIMARY KEY,
    key         TEXT NOT NULL,
    summary     TEXT NOT NULL,
    status      TEXT,
    embedding   BLOB,
    updated_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS users (
    email       TEXT PRIMARY KEY,
    user_id     TEXT,
    arr         REAL DEFAULT 0,
    plan_type   TEXT,
    company     TEXT
);
"""


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_conn() as conn:
        conn.executescript(DB_SCHEMA)
    log.info("Database initialized.")


# =============================================================================
# ENGINE 0B: EMBEDDING SINGLETON
# =============================================================================

class EmbeddingEngine:
    """Thin adapter over the configured embedding provider (see llm.get_embedder).

    Returns float32 numpy arrays so callers can persist them as BLOBs and feed
    them straight into FAISS.
    """

    _instance: EmbeddingEngine | None = None
    _lock = threading.Lock()

    def __init__(self):
        self._embedder = get_embedder()

    @classmethod
    def get(cls) -> EmbeddingEngine:
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def encode(self, texts: list[str]) -> np.ndarray:
        return np.array(self._embedder.embed(texts), dtype="float32")

    def encode_one(self, text: str) -> np.ndarray:
        return self.encode([text])[0]


# =============================================================================
# ENGINE 1: UNIFIED INGESTION ENGINE  (The "Hook")
# =============================================================================

@dataclass
class StandardFeedbackObject:
    """
    Every piece of feedback — regardless of source — is normalized to this.
    Metadata is always preserved.
    """
    id:         str = field(default_factory=lambda: str(uuid.uuid4()))
    text:       str = ""
    user_id:    str = ""
    user_email: str = ""
    source:     str = ""          # "zendesk" | "app_store" | "play_store" | "slack" | "webhook"
    timestamp:  str = field(default_factory=lambda: datetime.utcnow().isoformat())
    raw_meta:   dict = field(default_factory=dict)   # ← full original payload preserved
    labels:     list[str] = field(default_factory=list)
    arr:        float = 0.0
    plan_type:  str = ""
    jira_ticket: str = ""
    jira_status: str = ""


class Normalizer:
    """Converts raw source payloads → StandardFeedbackObject."""

    @staticmethod
    def from_zendesk(ticket: dict) -> StandardFeedbackObject:
        requester = ticket.get("requester", {})
        return StandardFeedbackObject(
            text=ticket.get("description", ""),
            user_id=str(requester.get("id", "")),
            user_email=requester.get("email", ""),
            source="zendesk",
            timestamp=ticket.get("created_at", datetime.utcnow().isoformat()),
            raw_meta=ticket,
        )

    @staticmethod
    def from_app_store(review: dict) -> StandardFeedbackObject:
        return StandardFeedbackObject(
            text=f"{review.get('title', '')} {review.get('content', '')}".strip(),
            user_id=str(review.get("userName", "")),
            user_email="",
            source="app_store",
            timestamp=review.get("date", datetime.utcnow().isoformat()),
            raw_meta=review,
        )

    @staticmethod
    def from_play_store(review: dict) -> StandardFeedbackObject:
        return StandardFeedbackObject(
            text=review.get("content", ""),
            user_id=str(review.get("userName", "")),
            user_email="",
            source="play_store",
            timestamp=review.get("at", datetime.utcnow().isoformat()),
            raw_meta=review,
        )

    @staticmethod
    def from_slack(event: dict) -> StandardFeedbackObject:
        return StandardFeedbackObject(
            text=event.get("text", ""),
            user_id=event.get("user", ""),
            user_email="",
            source="slack",
            timestamp=datetime.utcfromtimestamp(
                float(event.get("ts", time.time()))
            ).isoformat(),
            raw_meta=event,
        )

    @staticmethod
    def from_webhook(payload: dict) -> StandardFeedbackObject:
        """Generic webhook — maps common field names automatically."""
        text = (
            payload.get("text") or
            payload.get("message") or
            payload.get("body") or
            payload.get("feedback") or
            str(payload)
        )
        return StandardFeedbackObject(
            text=text,
            user_id=str(payload.get("user_id", payload.get("userId", ""))),
            user_email=payload.get("email", payload.get("user_email", "")),
            source=payload.get("source", "webhook"),
            timestamp=payload.get("timestamp", datetime.utcnow().isoformat()),
            raw_meta=payload,
        )


def save_feedback(fb: StandardFeedbackObject):
    """Persist a StandardFeedbackObject to the database."""
    embedding = EmbeddingEngine.get().encode_one(fb.text)
    with get_conn() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO feedback
              (id, text, user_id, user_email, source, timestamp,
               raw_meta, labels, arr, plan_type, jira_ticket, jira_status, embedding)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                fb.id, fb.text, fb.user_id, fb.user_email,
                fb.source, fb.timestamp,
                json.dumps(fb.raw_meta),
                json.dumps(fb.labels),
                fb.arr, fb.plan_type,
                fb.jira_ticket, fb.jira_status,
                embedding.tobytes(),
            ),
        )
    log.info(f"Saved feedback [{fb.source}] id={fb.id}")
    return fb


# ---- Component A: Polling Services ----------------------------------------

def poll_zendesk():
    """Fetch new Zendesk tickets from the last hour."""
    if not all([settings.zendesk_url, settings.zendesk_email, settings.zendesk_token]):
        log.debug("Zendesk not configured, skipping poll.")
        return
    since = (datetime.utcnow() - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    url = f"{settings.zendesk_url}/api/v2/tickets.json?sort_by=created_at&sort_order=desc"
    try:
        resp = requests.get(
            url,
            auth=(f"{settings.zendesk_email}/token", settings.zendesk_token),
            timeout=30,
        )
        resp.raise_for_status()
        tickets = resp.json().get("tickets", [])
        count = 0
        for t in tickets:
            if t.get("created_at", "") >= since:
                fb = Normalizer.from_zendesk(t)
                pipeline(fb)
                count += 1
        log.info(f"Zendesk poll: ingested {count} tickets.")
    except Exception as e:
        log.error(f"Zendesk poll failed: {e}")


def poll_app_store():
    """Fetch recent iOS App Store reviews."""
    if not settings.app_store_app_id:
        return
    try:
        from app_store_scraper import AppStore  # pip install app-store-scraper
        app = AppStore(country="us", app_name="", app_id=settings.app_store_app_id)
        app.review(how_many=50)
        for review in app.reviews:
            fb = Normalizer.from_app_store(review)
            pipeline(fb)
        log.info(f"App Store poll: ingested {len(app.reviews)} reviews.")
    except ImportError:
        log.warning("app-store-scraper not installed. Run: pip install app-store-scraper")
    except Exception as e:
        log.error(f"App Store poll failed: {e}")


def poll_play_store():
    """Fetch recent Google Play reviews."""
    if not settings.play_store_app_id:
        return
    try:
        from google_play_scraper import Sort, reviews  # pip install google-play-scraper
        result, _ = reviews(
            settings.play_store_app_id, lang="en", country="us", sort=Sort.NEWEST, count=50
        )
        for review in result:
            fb = Normalizer.from_play_store(review)
            pipeline(fb)
        log.info(f"Play Store poll: ingested {len(result)} reviews.")
    except ImportError:
        log.warning("google-play-scraper not installed. Run: pip install google-play-scraper")
    except Exception as e:
        log.error(f"Play Store poll failed: {e}")


def poll_slack_channel(channel_id: str):
    """Fetch recent messages from a Slack channel."""
    if not settings.slack_bot_token:
        return
    try:
        from slack_sdk import WebClient  # pip install slack_sdk
        client = WebClient(token=settings.slack_bot_token)
        oldest = str(time.time() - 3600)
        result = client.conversations_history(channel=channel_id, oldest=oldest)
        messages = result.get("messages", [])
        for msg in messages:
            if msg.get("subtype"):        # skip bot/system messages
                continue
            fb = Normalizer.from_slack(msg)
            pipeline(fb)
        log.info(f"Slack poll [{channel_id}]: ingested {len(messages)} messages.")
    except ImportError:
        log.warning("slack_sdk not installed. Run: pip install slack_sdk")
    except Exception as e:
        log.error(f"Slack poll failed: {e}")


# =============================================================================
# ENGINE 2: ADAPTIVE TAXONOMY ENGINE  (The "Brain")
# =============================================================================

def call_llm(prompt: str, expect_json: bool = False) -> str:
    """Call the configured LLM provider. Falls back to an empty string on failure."""
    try:
        text = get_llm().generate(prompt).strip()
        if expect_json:
            # Strip markdown fences
            text = re.sub(r"^```[a-z]*\n?", "", text)
            text = re.sub(r"\n?```$", "", text)
        return text
    except Exception as e:
        log.error(f"LLM error: {e}")
        return ""

# =============================================================================
# LLM JSON RECOVERY LAYER  (CRITICAL FIX)
# =============================================================================

def extract_json_from_llm(text: str):
    """
    Recover first valid JSON object from messy LLM output.
    Handles markdown, explanations, and formatting noise.
    """
    if not text:
        return None

    # remove markdown fences
    text = re.sub(r"```json", "", text, flags=re.IGNORECASE)
    text = re.sub(r"```", "", text)

    start = text.find("{")
    end = text.rfind("}") + 1

    if start == -1 or end <= 0:
        return None

    candidate = text[start:end]

    try:
        return json.loads(candidate)
    except Exception as e:
        log.error(f"JSON recovery failed: {e}")
        return None


def normalize_taxonomy_nodes(data: dict):
    """
    Normalize schema variations from LLM output.
    Makes system tolerant to model drift.
    """
    if not data:
        return []

    nodes = (
        data.get("taxonomy")
        or data.get("categories")
        or data.get("features")
        or []
    )

    cleaned = []

    for node in nodes:
        if not isinstance(node, dict):
            continue

        cleaned.append({
            "level1": node.get("level1") or node.get("category"),
            "level2": node.get("level2"),
            "level3": node.get("level3"),
            "description": node.get("description", "")
        })

    return cleaned

# ---- Component A: Bootstrapper --------------------------------------------

def bootstrap_taxonomy_from_url(url: str):
    """
    Scrape a public URL (help docs / pricing page) and ask the LLM to build
    a 3-level taxonomy. Stores results in the taxonomy table.
    """
    log.info(f"Bootstrapping taxonomy from: {url}")
    try:
        resp = requests.get(url, timeout=30)
        soup = BeautifulSoup(resp.text, "html.parser")
        # Extract readable text, skip nav/footer noise
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        page_text = soup.get_text(separator=" ", strip=True)[:4000]
    except Exception as e:
        log.error(f"Failed to scrape {url}: {e}")
        return

    prompt = f"""
You are a product taxonomy expert.

Given the following product documentation text, generate a 3-level hierarchical
taxonomy of product features and areas. Return ONLY valid JSON in this exact format:

{{
  "taxonomy": [
    {{
      "level1": "Authentication",
      "level2": "Login",
      "level3": "SSO",
      "description": "Single sign-on login flows"
    }},
    ...
  ]
}}

Product text:
{page_text}
"""
    response = call_llm(prompt, expect_json=True)
    if not response:
        log.error("No taxonomy response from LLM.")
        return

    try:
        data = extract_json_from_llm(response)
        nodes = normalize_taxonomy_nodes(data)

        if not nodes:
            log.error("LLM returned empty or invalid taxonomy. Keeping existing taxonomy.")
            return

        with get_conn() as conn:
            conn.execute("DELETE FROM taxonomy")  # reset on re-bootstrap
            for node in nodes:
                conn.execute(
                    "INSERT INTO taxonomy (id, level1, level2, level3, description) VALUES (?,?,?,?,?)",
                    (str(uuid.uuid4()), node.get("level1"), node.get("level2"),
                     node.get("level3"), node.get("description")),
                )
        log.info(f"Taxonomy bootstrapped with {len(nodes)} nodes.")
    except Exception as e:
        log.error(f"Taxonomy parse failed: {e}\nRaw: {response}")


def get_taxonomy_labels() -> list[str]:
    """Return flat list of 'Level1 > Level2 > Level3' strings for classification."""
    with get_conn() as conn:
        rows = conn.execute("SELECT level1, level2, level3 FROM taxonomy").fetchall()
    labels = []
    for r in rows:
        parts = [r["level1"], r["level2"], r["level3"]]
        labels.append(" > ".join(p for p in parts if p))
    return labels or ["General > Feedback > Uncategorized"]


# ---- Component B: Classifier (Multi-label) --------------------------------

def classify_feedback(fb: StandardFeedbackObject) -> list[str]:
    """
    Multi-label classification: one review can map to MULTIPLE taxonomy nodes.
    e.g. "Login failed and support was rude" → [Authentication > Login, Support > Quality]
    """
    labels = get_taxonomy_labels()
    labels_text = "\n".join(f"- {l}" for l in labels)

    prompt = f"""
You are a product feedback classifier.

Taxonomy options (pick ALL that apply):
{labels_text}

Feedback text:
"{fb.text}"

Rules:
- Return ONLY valid JSON: {{"labels": ["Label A", "Label B"]}}
- A single review can have MULTIPLE labels (multi-label classification).
- If nothing fits, use: {{"labels": ["General > Feedback > Uncategorized"]}}
- No explanation, just JSON.
"""
    response = call_llm(prompt, expect_json=True)
    try:
        data = extract_json_from_llm(response)

        if not data:
            return ["General > Feedback > Uncategorized"]

        labels = data.get("labels")

        if not isinstance(labels, list) or not labels:
            return ["General > Feedback > Uncategorized"]

        return labels

    except Exception:
        return ["General > Feedback > Uncategorized"]


# ---- Component C: Drift Detector ------------------------------------------

def detect_taxonomy_drift():
    """
    Weekly: cluster 'Unclassified' feedback using HDBSCAN to find emerging topics.
    Suggests new taxonomy nodes to add.
    """
    try:
        import hdbscan  # pip install hdbscan
    except ImportError:
        log.warning("hdbscan not installed. Run: pip install hdbscan")
        return

    log.info("Running taxonomy drift detection...")
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, text, embedding FROM feedback
            WHERE labels LIKE '%Uncategorized%' AND embedding IS NOT NULL
            """
        ).fetchall()

    if len(rows) < 10:
        log.info("Not enough unclassified feedback for drift detection (need ≥10).")
        return

    texts = [r["text"] for r in rows]
    embeddings = np.array(
        [np.frombuffer(r["embedding"], dtype="float32") for r in rows]
    )

    # HDBSCAN clustering — fully unsupervised, finds arbitrary cluster shapes
    clusterer = hdbscan.HDBSCAN(
        min_cluster_size=settings.drift_min_cluster_size, metric="euclidean"
    )
    labels = clusterer.fit_predict(embeddings)
    unique_clusters = set(labels) - {-1}  # -1 = noise

    suggestions = []
    for cluster_id in unique_clusters:
        mask = labels == cluster_id
        cluster_texts = [texts[i] for i, m in enumerate(mask) if m]
        sample = " | ".join(cluster_texts[:5])

        prompt = f"""
Given these user feedback samples from the same cluster, suggest a concise
3-level taxonomy label in the format "Level1 > Level2 > Level3".

Samples:
{sample}

Return ONLY the label string, nothing else.
"""
        suggestion = call_llm(prompt).strip().strip('"')
        if suggestion:
            suggestions.append({"cluster_size": int(mask.sum()), "suggested_label": suggestion})
            log.info(f"Drift detected → Suggested: '{suggestion}' ({int(mask.sum())} items)")

    if suggestions:
        log.info(f"Drift report: {json.dumps(suggestions, indent=2)}")
        # In production: send to dashboard or Slack
        send_slack_alert(
            f"📊 *Taxonomy Drift Report* — {len(suggestions)} new emerging topics detected:\n"
            + "\n".join(f"  • `{s['suggested_label']}` ({s['cluster_size']} items)" for s in suggestions)
        )
    return suggestions


# =============================================================================
# ENGINE 3: REVENUE IMPACT ENGINE  (The "Ranker")
# =============================================================================

# ---- Component A: User Identity Resolver ----------------------------------

def resolve_user_revenue(fb: StandardFeedbackObject) -> StandardFeedbackObject:
    """
    Match user email → ARR + plan type from local users table.
    Populate from CRM sync (Component B below) or manual CSV import.
    """
    if not fb.user_email:
        return fb
    with get_conn() as conn:
        row = conn.execute(
            "SELECT arr, plan_type FROM users WHERE email = ?", (fb.user_email,)
        ).fetchone()
    if row:
        fb.arr = row["arr"] or 0.0
        fb.plan_type = row["plan_type"] or ""
    return fb


def import_users_from_csv(csv_path: str):
    """
    Bulk-import users from a CSV with columns: email, user_id, arr, plan_type, company.
    Free alternative to CRM API — just export from HubSpot/Salesforce as CSV.
    """
    import csv
    count = 0
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        with get_conn() as conn:
            for row in reader:
                conn.execute(
                    "INSERT OR REPLACE INTO users (email, user_id, arr, plan_type, company) VALUES (?,?,?,?,?)",
                    (
                        row.get("email", ""),
                        row.get("user_id", ""),
                        float(row.get("arr", 0) or 0),
                        row.get("plan_type", ""),
                        row.get("company", ""),
                    ),
                )
                count += 1
    log.info(f"Imported {count} users from {csv_path}")


# ---- Component B: CRM Sync (HubSpot free tier) ----------------------------

def sync_hubspot_contacts():
    """
    Sync HubSpot contacts (free tier) → populate users table with ARR / plan type.
    Uses HubSpot's free public API (no paid plan required for basic contact properties).
    Set env: HUBSPOT_API_KEY
    """
    api_key = settings.hubspot_api_key
    if not api_key:
        log.debug("HubSpot not configured, skipping CRM sync.")
        return

    url = "https://api.hubapi.com/crm/v3/objects/contacts"
    params = {
        "limit": 100,
        "properties": "email,hs_analytics_revenue,jobtitle,company",
        "hapikey": api_key,
    }
    try:
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        contacts = resp.json().get("results", [])
        with get_conn() as conn:
            for c in contacts:
                props = c.get("properties", {})
                email = props.get("email", "")
                if not email:
                    continue
                arr = float(props.get("hs_analytics_revenue", 0) or 0)
                plan = props.get("jobtitle", "")
                company = props.get("company", "")
                conn.execute(
                    "INSERT OR REPLACE INTO users (email, arr, plan_type, company) VALUES (?,?,?,?)",
                    (email, arr, plan, company),
                )
        log.info(f"HubSpot sync: updated {len(contacts)} contacts.")
    except Exception as e:
        log.error(f"HubSpot sync failed: {e}")


# ---- Component C: Revenue Aggregator --------------------------------------

def get_revenue_impact_report(top_n: int = 20) -> list[dict]:
    """
    Issue_Score = COUNT(unique_users) * SUM(user_ARR)
    Returns ranked list: "Fixing X is worth $Y/month across Z users."
    """
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT
                labels,
                COUNT(DISTINCT user_email) AS user_count,
                COALESCE(SUM(arr), 0)       AS total_arr,
                COUNT(*)                    AS feedback_count
            FROM feedback
            WHERE labels != '[]'
            GROUP BY labels
            ORDER BY (COUNT(DISTINCT user_email) * COALESCE(SUM(arr), 0)) DESC
            LIMIT ?
            """,
            (top_n,),
        ).fetchall()

    report = []
    for row in rows:
        try:
            label_list = json.loads(row["labels"])
        except Exception:
            label_list = [row["labels"]]

        score = row["user_count"] * row["total_arr"]
        report.append({
            "labels": label_list,
            "user_count": row["user_count"],
            "total_arr": round(row["total_arr"], 2),
            "feedback_count": row["feedback_count"],
            "issue_score": round(score, 2),
            "summary": (
                f"Fixing '{', '.join(label_list)}' is worth "
                f"${score:,.0f}/year across {row['user_count']} users."
            ),
        })
    return report


# =============================================================================
# ENGINE 4: ROADMAP DEDUPLICATION ENGINE  (The "Loop")
# =============================================================================

class VectorStore:
    """
    In-process FAISS index over Jira ticket embeddings.
    Free replacement for Pinecone / pgvector.
    """

    def __init__(self):
        self.index: faiss.Index | None = None
        self.ticket_keys: list[str] = []   # parallel list: index position → jira key
        self._lock = threading.Lock()

    def rebuild_from_db(self):
        """Load all Jira ticket embeddings from SQLite into FAISS."""
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT key, embedding FROM jira_tickets WHERE embedding IS NOT NULL"
            ).fetchall()

        if not rows:
            log.info("VectorStore: no tickets to index.")
            return

        embeddings = np.array(
            [np.frombuffer(r["embedding"], dtype="float32") for r in rows],
            dtype="float32",
        )
        dim = embeddings.shape[1]
        self.index = faiss.IndexFlatIP(dim)   # Inner product = cosine on normalized vecs
        faiss.normalize_L2(embeddings)
        self.index.add(embeddings)
        self.ticket_keys = [r["key"] for r in rows]
        log.info(f"VectorStore: indexed {len(rows)} Jira tickets.")

    def search(self, query_vec: np.ndarray, k: int = 3) -> list[tuple[str, float]]:
        """Returns list of (jira_key, similarity_score)."""
        if self.index is None or not self.ticket_keys:
            return []
        k = min(k, len(self.ticket_keys))
        q = query_vec.reshape(1, -1).copy().astype("float32")
        faiss.normalize_L2(q)
        scores, indices = self.index.search(q, k)
        results = []
        for score, idx in zip(scores[0], indices[0]):
            if idx >= 0:
                results.append((self.ticket_keys[idx], float(score)))
        return results


VECTOR_STORE = VectorStore()


# ---- Component A: Jira / Linear Sync -------------------------------------

def sync_jira_tickets():
    """
    Fetch In Progress + Backlog tickets from Jira, embed them, store in SQLite.
    Linear users: swap for Linear GraphQL API (also free).
    """
    if not all([settings.jira_url, settings.jira_email, settings.jira_token]):
        if settings.load_demo_data:
            log.info("Jira not configured — loading demo tickets (PRODRION_LOAD_DEMO_DATA=true).")
            _load_demo_tickets()
        else:
            log.info("Jira not configured — skipping ticket sync.")
        return

    jql = (
        f"project={settings.jira_project} "
        "AND status in ('In Progress','Backlog') ORDER BY updated DESC"
    )
    url = f"{settings.jira_url}/rest/api/3/search"
    try:
        resp = requests.get(
            url,
            params={"jql": jql, "maxResults": 200, "fields": "summary,status"},
            auth=(settings.jira_email, settings.jira_token),
            timeout=30,
        )
        resp.raise_for_status()
        issues = resp.json().get("issues", [])
        _upsert_tickets(issues)
        VECTOR_STORE.rebuild_from_db()
        log.info(f"Jira sync: indexed {len(issues)} tickets.")
    except Exception as e:
        log.error(f"Jira sync failed: {e}")


def _upsert_tickets(issues: list[dict]):
    embedder = EmbeddingEngine.get()
    with get_conn() as conn:
        for issue in issues:
            key = issue.get("key", "")
            fields = issue.get("fields", {})
            summary = fields.get("summary", "")
            status = fields.get("status", {}).get("name", "")
            embedding = embedder.encode_one(f"{key}: {summary}")
            conn.execute(
                """
                INSERT OR REPLACE INTO jira_tickets (id, key, summary, status, embedding, updated_at)
                VALUES (?,?,?,?,?,datetime('now'))
                """,
                (str(uuid.uuid4()), key, summary, status, embedding.tobytes()),
            )


def _load_demo_tickets():
    """Load a few demo tickets so the system works without Jira credentials."""
    demo = [
        {"key": "PROJ-101", "fields": {"summary": "Fix SSO timeout on login", "status": {"name": "In Progress"}}},
        {"key": "PROJ-102", "fields": {"summary": "Export to CSV broken for large datasets", "status": {"name": "Backlog"}}},
        {"key": "PROJ-103", "fields": {"summary": "Mobile app crashes on startup", "status": {"name": "Backlog"}}},
        {"key": "PROJ-104", "fields": {"summary": "Support for multi-factor authentication", "status": {"name": "In Progress"}}},
    ]
    _upsert_tickets(demo)
    VECTOR_STORE.rebuild_from_db()
    log.info("Loaded demo Jira tickets.")


# ---- Component C: Semantic Matcher ----------------------------------------

def match_to_jira(fb: StandardFeedbackObject) -> StandardFeedbackObject:
    """
    If cosine similarity > settings.similarity_threshold, auto-link feedback to a Jira ticket.
    """
    if not fb.text:
        return fb
    vec = EmbeddingEngine.get().encode_one(fb.text)
    matches = VECTOR_STORE.search(vec, k=3)
    for jira_key, score in matches:
        if score >= settings.similarity_threshold:
            with get_conn() as conn:
                row = conn.execute(
                    "SELECT status FROM jira_tickets WHERE key=?", (jira_key,)
                ).fetchone()
            fb.jira_ticket = jira_key
            fb.jira_status = row["status"] if row else "Unknown"
            log.info(
                f"Linked feedback to {jira_key} (similarity={score:.3f}, status={fb.jira_status})"
            )
            break   # link to the best match only
    return fb


# =============================================================================
# ENGINE 5: DASHBOARD & INTELLIGENCE LAYER  (The "UI")
# =============================================================================

def send_slack_alert(message: str):
    """Send a Slack message using the free Slack Bot Token."""
    if not settings.slack_bot_token:
        log.info(f"[Slack alert - not configured]: {message}")
        return
    try:
        from slack_sdk import WebClient
        client = WebClient(token=settings.slack_bot_token)
        client.chat_postMessage(channel=settings.slack_alert_channel, text=message)
    except ImportError:
        log.warning("slack_sdk not installed.")
    except Exception as e:
        log.error(f"Slack alert failed: {e}")


# ---- Component A: Text-to-SQL Query Interface ----------------------------

def chat_with_data(natural_language_query: str) -> dict:
    """
    Converts a natural language question into SQL, runs it on SQLite,
    returns results. Uses local Ollama instead of GPT-4 — 100% free.

    Example:
      "Show me all login issues from Enterprise customers last week"
    """
    schema_hint = """
Table: feedback
  id, text, user_id, user_email, source, timestamp,
  labels (JSON array as text), arr, plan_type,
  jira_ticket, jira_status, created_at

Table: taxonomy
  level1, level2, level3, description

Table: users
  email, user_id, arr, plan_type, company
"""

    prompt = f"""
You are an expert SQLite query generator.

Database schema:
{schema_hint}

User question: "{natural_language_query}"

Rules:
- Generate ONLY the SQL SELECT query, nothing else.
- No markdown, no explanation.
- Use proper SQLite syntax (e.g., datetime(), strftime()).
- Labels is a JSON text field — use LIKE '%value%' for filtering.
- Limit results to 100 rows.
"""
    sql = call_llm(prompt).strip().rstrip(";").strip()
    sql = re.sub(r"^```[a-z]*\n?", "", sql)
    sql = re.sub(r"\n?```$", "", sql).strip()

    if not sql.upper().startswith("SELECT"):
        return {"error": "LLM did not produce a valid SELECT query.", "raw": sql}

    try:
        with get_conn() as conn:
            cursor = conn.execute(sql)
            columns = [d[0] for d in cursor.description]
            rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
        return {"query": sql, "columns": columns, "rows": rows, "count": len(rows)}
    except Exception as e:
        return {"error": str(e), "query": sql}


# ---- Component B: Trend / Velocity Watcher --------------------------------

def check_velocity_spikes():
    """
    Compares this week's volume vs last week's per taxonomy label.
    Fires a Slack alert if volume doubles or more.
    """
    log.info("Checking velocity spikes...")
    now = datetime.utcnow()
    week_ago  = (now - timedelta(days=7)).isoformat()
    two_weeks = (now - timedelta(days=14)).isoformat()

    with get_conn() as conn:
        # This week
        this_week = conn.execute(
            """
            SELECT labels, COUNT(*) as cnt
            FROM feedback
            WHERE timestamp >= ?
            GROUP BY labels
            """,
            (week_ago,),
        ).fetchall()

        # Last week
        last_week = conn.execute(
            """
            SELECT labels, COUNT(*) as cnt
            FROM feedback
            WHERE timestamp >= ? AND timestamp < ?
            GROUP BY labels
            """,
            (two_weeks, week_ago),
        ).fetchall()

    last_dict = {r["labels"]: r["cnt"] for r in last_week}

    for row in this_week:
        label_key = row["labels"]
        this_cnt  = row["cnt"]
        last_cnt  = last_dict.get(label_key, 0)

        if last_cnt == 0:
            continue   # skip brand-new labels (no baseline)

        if this_cnt >= last_cnt * settings.velocity_spike_mult:
            try:
                label_display = ", ".join(json.loads(label_key))
            except Exception:
                label_display = label_key

            msg = (
                f"🚨 *Velocity Spike Detected!*\n"
                f"Label: `{label_display}`\n"
                f"Last week: {last_cnt} → This week: {this_cnt} "
                f"({this_cnt/max(last_cnt,1):.1f}x increase)"
            )
            log.warning(msg)
            send_slack_alert(msg)

def taxonomy_exists() -> bool:
    with get_conn() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM taxonomy"
        ).fetchone()[0]
    return count > 0


def auto_bootstrap_taxonomy_if_needed(source: str):
    """
    Build the taxonomy once, from the configured product docs URL, when the
    taxonomy table is empty. Does nothing if PRODRION_TAXONOMY_BOOTSTRAP_URL
    is not set.
    """
    if taxonomy_exists():
        return

    if not settings.taxonomy_bootstrap_url:
        log.info(
            "Taxonomy empty and PRODRION_TAXONOMY_BOOTSTRAP_URL not set "
            "— skipping auto-bootstrap (triggered by source=%s).",
            source,
        )
        return

    log.info("Taxonomy empty → auto-bootstrapping from %s", settings.taxonomy_bootstrap_url)
    bootstrap_taxonomy_from_url(settings.taxonomy_bootstrap_url)

# =============================================================================
# THE PIPELINE: Wire all engines together
# =============================================================================

def pipeline(fb: StandardFeedbackObject) -> StandardFeedbackObject:
    """
    Full end-to-end processing pipeline for a single piece of feedback:
      1. Resolve revenue (Engine 3A)
      2. Classify taxonomy (Engine 2B)
      3. Match to Jira (Engine 4C)
      4. Save to database
    """
    auto_bootstrap_taxonomy_if_needed(fb.source)
    fb = resolve_user_revenue(fb)
    fb.labels = classify_feedback(fb)
    fb = match_to_jira(fb)
    save_feedback(fb)
    return fb


# =============================================================================
# FASTAPI WEBHOOK RECEIVER  (Engine 1B)
# =============================================================================

app = FastAPI(title="Feedback Intelligence Platform", version="1.0.0")


@app.post("/webhook/feedback")
async def webhook_feedback(request: Request):
    """Generic webhook endpoint. Accepts any JSON payload."""
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload.")
    fb = Normalizer.from_webhook(payload)
    fb = pipeline(fb)
    return {
        "status": "ingested",
        "id": fb.id,
        "labels": fb.labels,
        "jira_ticket": fb.jira_ticket,
        "jira_status": fb.jira_status,
    }


@app.post("/webhook/slack")
async def webhook_slack(request: Request):
    """Slack Events API endpoint."""
    payload = await request.json()
    # Slack URL verification challenge
    if "challenge" in payload:
        return {"challenge": payload["challenge"]}
    event = payload.get("event", {})
    if event.get("type") == "message" and not event.get("subtype"):
        fb = Normalizer.from_slack(event)
        pipeline(fb)
    return {"status": "ok"}


@app.get("/query")
async def query_data(q: str):
    """Natural language query interface. e.g. ?q=Show me all login issues from Enterprise users"""
    return chat_with_data(q)


@app.get("/revenue-impact")
async def revenue_impact(top_n: int = 20):
    """Returns issues ranked by revenue impact (COUNT * ARR)."""
    return {"report": get_revenue_impact_report(top_n)}


@app.post("/bootstrap-taxonomy")
async def bootstrap_taxonomy(url: str):
    """Scrape a URL and auto-generate the product taxonomy."""
    bootstrap_taxonomy_from_url(url)
    return {"status": "taxonomy bootstrapped", "url": url}


@app.get("/taxonomy")
async def get_taxonomy():
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM taxonomy").fetchall()
    return {"taxonomy": [dict(r) for r in rows]}


@app.post("/import-users")
async def import_users(csv_path: str):
    """Import users + ARR data from a CSV file path on disk."""
    if not os.path.exists(csv_path):
        raise HTTPException(status_code=404, detail=f"File not found: {csv_path}")
    import_users_from_csv(csv_path)
    return {"status": "imported", "file": csv_path}


@app.get("/health")
async def health():
    with get_conn() as conn:
        feedback_count = conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]
        ticket_count   = conn.execute("SELECT COUNT(*) FROM jira_tickets").fetchone()[0]
    return {
        "status": "ok",
        "feedback_count": feedback_count,
        "jira_tickets_indexed": ticket_count,
        "ollama_url": settings.ollama_url,
        "model": settings.ollama_model,
    }


# =============================================================================
# SCHEDULER: Hourly polling + weekly drift detection
# =============================================================================

def start_scheduler():
    scheduler = BackgroundScheduler()

    # Hourly: poll all sources
    scheduler.add_job(poll_zendesk,                  "interval", hours=1,  id="zendesk")
    scheduler.add_job(poll_app_store,                "interval", hours=1,  id="app_store")
    scheduler.add_job(poll_play_store,               "interval", hours=1,  id="play_store")
    scheduler.add_job(sync_jira_tickets,             "interval", hours=1,  id="jira_sync")
    scheduler.add_job(sync_hubspot_contacts,         "interval", hours=6,  id="hubspot_sync")
    scheduler.add_job(check_velocity_spikes,         "interval", hours=24, id="velocity")

    # Weekly: drift detection + taxonomy suggestions
    scheduler.add_job(detect_taxonomy_drift,         "interval", weeks=1,  id="drift")

    scheduler.start()
    log.info("Scheduler started.")
    return scheduler


# =============================================================================
# STARTUP
# =============================================================================

def startup():
    log.info("=" * 60)
    log.info("  FEEDBACK INTELLIGENCE PLATFORM")
    log.info("=" * 60)

    init_db()

    # Pre-load embedding model
    EmbeddingEngine.get()

    # Sync Jira tickets and build vector index on startup
    sync_jira_tickets()

    # Start background scheduler
    start_scheduler()

    log.info("Platform ready. Starting API server on http://0.0.0.0:8000")
    log.info("  POST /webhook/feedback  → ingest any feedback JSON")
    log.info("  POST /webhook/slack     → Slack Events API")
    log.info("  GET  /query?q=...       → natural language → SQL")
    log.info("  GET  /revenue-impact    → issues ranked by $ impact")
    log.info("  POST /bootstrap-taxonomy?url=...  → auto-build taxonomy")
    log.info("  GET  /health            → system status")


@app.on_event("startup")
async def on_startup():
    startup()


# =============================================================================
# ENTRY POINT
# =============================================================================

if __name__ == "__main__":
    uvicorn.run(
        "feedback_platform:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=False,
        log_level="info",
    )

