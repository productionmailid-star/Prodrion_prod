import os

import requests

REPO = "plausible/analytics"
URL = f"https://api.github.com/repos/{REPO}/issues"

resp = requests.get(URL, params={"state": "open", "per_page": 50})
issues = resp.json()

for issue in issues:
    text = issue["title"] + "\n" + (issue.get("body") or "")
    payload = {
        "text": text,
        "user_id": issue["user"]["login"],
        "source": "github",
        "external_id": str(issue["number"]),  # stable id -> re-runs are idempotent
        "timestamp": issue["created_at"],
    }

    requests.post(
        "http://localhost:8000/webhook/feedback",
        json=payload,
        headers={"X-Webhook-Secret": os.environ.get("PRODRION_WEBHOOK_SECRET", "")},
        timeout=30,
    )
