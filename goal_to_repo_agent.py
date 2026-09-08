"""
goal_to_repo_agent.py
---------------------
Unified pipeline connector.

  Feedback Platform  →  Goal Generator  →  Repo Agent Server  →  Console Report

Flow:
  1. Query the feedback platform API for all labelled feedback.
  2. Cluster labels by frequency (skip noise & singletons).
  3. Ask Ollama to turn each cluster into one measurable product goal.
  4. POST each goal to the repo-agent server (repo_agent_server.py).
  5. Consume the SSE stream and print a structured report.

Requirements:
  - Feedback platform API running on API_URL       (default: localhost:8000)
  - Repo agent server running on REPO_AGENT_URL    (default: localhost:7845)
    Start it with:  uvicorn repo_agent_server:app --port 7845
  - Ollama running with OLLAMA_MODEL available.

No internal files of either sub-system are modified.
"""

import json
import sys
from collections import Counter

import requests

from config import settings

# ── Config ───────────────────────────────────────────────────────────────────
# All values come from config.Settings (PRODRION_* env / .env).

API_URL          = settings.feedback_api_url
REPO_AGENT_URL   = settings.repo_agent_url
MIN_CLUSTER_SIZE = settings.goal_min_cluster_size

# ── Ollama call ───────────────────────────────────────────────────────────────

def call_ollama(prompt: str) -> str:
    resp = requests.post(
        settings.ollama_url,
        json={"model": settings.ollama_model, "prompt": prompt, "stream": False},
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()["response"].strip()


# ── Feedback platform: fetch labels ──────────────────────────────────────────

def fetch_all_labels() -> list[str]:
    """Pull every label stored in the feedback platform database."""
    try:
        resp = requests.get(f"{API_URL}/query", params={"q": "select labels from feedback"}, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        print(f"[ERROR] Cannot reach feedback platform at {API_URL}: {exc}")
        return []

    if "error" in data:
        print("[ERROR] Feedback platform query failed:", data["error"])
        return []

    all_labels: list[str] = []
    for row in data.get("rows", []):
        try:
            labels = json.loads(row.get("labels", "[]"))
            all_labels.extend(labels)
        except Exception:
            continue

    return all_labels


# ── Build clusters ────────────────────────────────────────────────────────────

def build_clusters(min_count: int = MIN_CLUSTER_SIZE) -> list[dict]:
    """Group labels by frequency; filter noise."""
    labels = fetch_all_labels()
    if not labels:
        return []

    counter = Counter(labels)
    clusters = []

    for label, count in counter.most_common():
        if "Uncategorized" in label:
            continue
        if count < min_count:
            continue
        clusters.append({"label": label, "feedback_count": count})

    print(f"[INFO] Found {len(clusters)} cluster(s) with ≥{min_count} feedback items.")
    return clusters


# ── Goal generation ───────────────────────────────────────────────────────────

def generate_goal(cluster: dict) -> str:
    """Ask Ollama to produce one measurable product goal for a feedback cluster."""
    prompt = f"""
You are a senior product manager.

Issue cluster:
  Label:          {cluster['label']}
  Feedback count: {cluster['feedback_count']}

Generate ONE clear, measurable product goal.

Rules:
- One sentence only.
- Must be actionable.
- Must include a measurable outcome.
- Focus on user impact.

Return ONLY valid JSON:
{{
  "goal": "..."
}}
"""
    response = call_ollama(prompt)

    try:
        start = response.find("{")
        end   = response.rfind("}") + 1
        data  = json.loads(response[start:end])
        return data["goal"]
    except Exception:
        # Fallback: return the raw text trimmed of markdown
        return response.strip().strip("`").strip()


# ── Repo agent: send goal and consume SSE stream ─────────────────────────────

def send_goal_to_repo_agent(goal: str) -> dict:
    """
    POST a goal to the repo-agent server and collect the SSE response.

    Returns:
        {
          "goal":    str,
          "plan":    dict | None,
          "results": list | None,
          "error":   str | None,   # present only on failure
        }
    """
    # ── Start pipeline ───────────────────────────────────────────────────
    try:
        resp = requests.post(
            f"{REPO_AGENT_URL}/api/run",
            json={"goal": goal},
            timeout=30,
        )
        resp.raise_for_status()
        run_data = resp.json()
    except requests.RequestException as exc:
        return {"goal": goal, "plan": None, "results": None,
                "error": f"Cannot reach repo-agent server at {REPO_AGENT_URL}: {exc}"}

    if not run_data.get("ok"):
        return {"goal": goal, "plan": None, "results": None,
                "error": run_data.get("message", "Unknown error from repo-agent")}

    session_id = run_data["session_id"]

    # ── Consume SSE stream ───────────────────────────────────────────────
    plan    = None
    results = None
    error   = None

    try:
        stream_resp = requests.get(
            f"{REPO_AGENT_URL}/api/stream/{session_id}",
            stream=True,
            timeout=600,   # pipeline can take a while
        )

        event_type = None

        for raw_line in stream_resp.iter_lines():
            if not raw_line:
                continue

            line = raw_line.decode("utf-8")

            if line.startswith("event:"):
                event_type = line.replace("event:", "").strip()
                continue

            if line.startswith("data:"):
                payload = line.replace("data:", "").strip()
                try:
                    data = json.loads(payload)
                except json.JSONDecodeError:
                    continue

                if event_type == "plan":
                    plan = data.get("plan")

                elif event_type == "results":
                    results = data.get("results")

                elif event_type == "done":
                    error = data.get("error")   # None on success
                    break

    except requests.RequestException as exc:
        error = f"Stream error: {exc}"

    return {
        "goal":    goal,
        "plan":    plan,
        "results": results,
        "error":   error,
    }


# ── Report printer ────────────────────────────────────────────────────────────

_SEP  = "=" * 70
_LINE = "-" * 40


def print_report(cluster: dict, goal: str, agent_result: dict):
    print(_SEP)
    print(f"CLUSTER  : {cluster['label']}  ({cluster['feedback_count']} feedback items)")
    print(f"GOAL     : {goal}")
    print()

    if agent_result.get("error"):
        print("[SKIPPED] Repo agent returned an error:")
        print(f"  {agent_result['error']}")
        return

    plan = agent_result.get("plan") or {}
    print("PLAN:")
    print(f"  Files to modify : {', '.join(plan.get('files_to_modify', [])) or '(none)'}")
    print(f"  Hits located    : {plan.get('hit_count', 0)}")
    print()

    results = agent_result.get("results") or []
    if not results:
        print("  (No patches generated)")
        return

    for i, entry in enumerate(results, 1):
        print(f"{_LINE}  Change {i}")
        for w in entry.get("warnings", []):
            print(f"  ⚠  {w}")
        print(entry.get("patch", "").strip())
        print()


# ── Main pipeline ─────────────────────────────────────────────────────────────

def run_pipeline():
    print("\n" + _SEP)
    print("  FEEDBACK → GOAL → REPO AGENT  Unified Pipeline")
    print(_SEP)

    # ── Health-check the repo agent server ───────────────────────────────
    try:
        hc = requests.get(f"{REPO_AGENT_URL}/api/health", timeout=5)
        hc.raise_for_status()
        print(f"[OK] Repo agent server reachable at {REPO_AGENT_URL}")
    except requests.RequestException:
        print(f"[ERROR] Repo agent server is NOT reachable at {REPO_AGENT_URL}")
        print("        Start it with:  uvicorn repo_agent_server:app --port 7845")
        sys.exit(1)

    # ── Build clusters from feedback platform ────────────────────────────
    clusters = build_clusters()

    if not clusters:
        print("[INFO] No clusters found. Nothing to process.")
        return

    # ── Process each cluster ─────────────────────────────────────────────
    for i, cluster in enumerate(clusters, 1):
        print(f"\n[{i}/{len(clusters)}] Processing cluster: {cluster['label']}")

        # 1. Generate goal
        print("  → Generating goal with Ollama...")
        goal = generate_goal(cluster)
        print(f"  → Goal: {goal}")

        # 2. Send to repo agent
        print("  → Sending goal to repo agent...")
        result = send_goal_to_repo_agent(goal)

        # 3. Print report
        print_report(cluster, goal, result)

    print(_SEP)
    print("Pipeline complete.")
    print(_SEP + "\n")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    run_pipeline()
