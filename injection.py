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
        "timestamp": issue["created_at"]
    }

    requests.post("http://localhost:8000/webhook/feedback", json=payload)
