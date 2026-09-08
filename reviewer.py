"""
Multi-Platform Review Fetcher + Negative Review Filter
=======================================================
Fetches reviews from:
  - Google Play Store
  - Apple App Store  (via iTunes RSS API)
  - Reddit

Then filters to keep ONLY negative reviews using:
  - PRIMARY:  DistilBERT (distilbert-base-uncased-finetuned-sst-2-english)
              Tiny 67MB transformer model, runs fully locally, very accurate.
  - FALLBACK: VADER (vaderSentiment) — rule-based, zero download, instant.

INSTALL DEPENDENCIES:
    pip install google-play-scraper requests vaderSentiment

  For DistilBERT (recommended, better accuracy):
    pip install transformers torch
    (model auto-downloads ~67MB on first run, cached after that)
"""

import requests
import json
import time
import re
import urllib.parse
from datetime import datetime, timezone


# ══════════════════════════════════════════════════════════
# SENTIMENT FILTER  —  negative review detection
# ══════════════════════════════════════════════════════════

class SentimentFilter:
    """
    Classifies reviews as NEGATIVE or not.

    Strategy (in order of preference):
      1. If review has a numeric star rating → use that directly (1-2★ = negative)
      2. DistilBERT transformer model       → ML-based, most accurate on text
      3. VADER                              → rule-based fallback, no download needed
    """

    def __init__(self, model: str = "auto"):
        """
        model: "distilbert" | "vader" | "auto"
               "auto" tries DistilBERT first, falls back to VADER if not installed.
        """
        self.model_type = None
        self._classifier = None

        if model in ("distilbert", "auto"):
            self._try_load_distilbert()

        if self.model_type is None and model in ("vader", "auto"):
            self._try_load_vader()

        if self.model_type is None:
            raise RuntimeError(
                "No sentiment model available. Install at least one:\n"
                "  pip install vaderSentiment\n"
                "  pip install transformers torch"
            )

    def _try_load_distilbert(self):
        try:
            from transformers import pipeline
            print("[Sentiment] Loading DistilBERT model (downloads ~67MB on first run)...")
            self._classifier = pipeline(
                "sentiment-analysis",
                model="distilbert-base-uncased-finetuned-sst-2-english",
                truncation=True,
                max_length=512,
            )
            self.model_type = "distilbert"
            print("[Sentiment] DistilBERT loaded ✓")
        except ImportError:
            print("[Sentiment] transformers/torch not found, trying VADER...")
        except Exception as e:
            print(f"[Sentiment] DistilBERT failed ({e}), trying VADER...")

    def _try_load_vader(self):
        try:
            from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
            self._classifier = SentimentIntensityAnalyzer()
            self.model_type = "vader"
            print("[Sentiment] VADER loaded ✓")
        except ImportError:
            print("[Sentiment] VADER not found. Run: pip install vaderSentiment")

    def is_negative(self, review: dict, threshold: float = 0.6) -> bool:
        """
        Returns True if the review is negative.

        threshold: confidence cutoff for ML model (0.0–1.0).
                   Lower = catch more negatives (but more false positives).
                   Higher = only very clearly negative reviews pass through.
        """
        # ── Fast path: use star rating if available ──────────────────────
        rating = review.get("rating")
        if rating is not None:
            try:
                return int(rating) <= 2
            except (ValueError, TypeError):
                pass

        # ── Text-based classification ─────────────────────────────────────
        text = f"{review.get('title', '')} {review.get('content', '')}".strip()
        if not text or text == "[Link post]":
            return False  # skip empty / link-only posts

        if self.model_type == "distilbert":
            return self._distilbert_is_negative(text, threshold)
        elif self.model_type == "vader":
            return self._vader_is_negative(text)
        return False

    def _distilbert_is_negative(self, text: str, threshold: float) -> bool:
        result = self._classifier(text[:512])[0]
        label = result["label"]   # "NEGATIVE" or "POSITIVE"
        score = result["score"]   # confidence 0–1
        return label == "NEGATIVE" and score >= threshold

    def _vader_is_negative(self, text: str) -> bool:
        scores = self._classifier.polarity_scores(text)
        # compound score: -1 (most negative) to +1 (most positive)
        # threshold of -0.05 catches mildly negative text too
        return scores["compound"] <= -0.05

    def score(self, review: dict) -> dict:
        """
        Returns a dict with sentiment label + confidence score.
        Useful for debugging or saving enriched data.
        """
        rating = review.get("rating")
        if rating is not None:
            try:
                r = int(rating)
                label = "NEGATIVE" if r <= 2 else ("NEUTRAL" if r == 3 else "POSITIVE")
                return {"label": label, "score": 1.0, "method": "star_rating"}
            except (ValueError, TypeError):
                pass

        text = f"{review.get('title', '')} {review.get('content', '')}".strip()
        if not text or text == "[Link post]":
            return {"label": "UNKNOWN", "score": 0.0, "method": "empty"}

        if self.model_type == "distilbert":
            result = self._classifier(text[:512])[0]
            return {
                "label": result["label"],
                "score": round(result["score"], 4),
                "method": "distilbert",
            }
        elif self.model_type == "vader":
            scores = self._classifier.polarity_scores(text)
            compound = scores["compound"]
            if compound <= -0.05:
                label = "NEGATIVE"
            elif compound >= 0.05:
                label = "POSITIVE"
            else:
                label = "NEUTRAL"
            return {
                "label": label,
                "score": round(abs(compound), 4),
                "compound": round(compound, 4),
                "method": "vader",
            }
        return {"label": "UNKNOWN", "score": 0.0, "method": "none"}


def filter_negative_reviews(
    all_reviews: dict,
    model: str = "auto",
    threshold: float = 0.6,
    add_sentiment_scores: bool = False,
) -> dict:
    """
    Takes the full reviews dict from fetch_all_reviews() and returns
    a new dict containing ONLY negative reviews from each source.

    Args:
        all_reviews:          Output of fetch_all_reviews()
        model:                "auto" | "distilbert" | "vader"
        threshold:            Confidence cutoff for DistilBERT (ignored for VADER/star rating)
        add_sentiment_scores: If True, adds a 'sentiment' key to each review dict

    Returns:
        Dict with same structure but only negative reviews + updated summary
    """
    print(f"\n[Sentiment Filter] Initialising model (mode: {model})...")
    sf = SentimentFilter(model=model)
    print(f"[Sentiment Filter] Using: {sf.model_type}")

    negative = {}
    sources = ["google_play", "app_store", "reddit"]

    for source in sources:
        reviews_list = all_reviews.get(source, [])
        if not reviews_list:
            negative[source] = []
            continue

        print(f"[Sentiment Filter] Scanning {len(reviews_list)} {source} reviews...")
        neg_list = []
        for r in reviews_list:
            sentiment = sf.score(r)
            if sentiment["label"] == "NEGATIVE":
                if add_sentiment_scores:
                    r = {**r, "sentiment": sentiment}
                neg_list.append(r)

        print(f"[Sentiment Filter] {source}: {len(neg_list)}/{len(reviews_list)} are negative")
        negative[source] = neg_list

    # ── Updated summary ───────────────────────────────────────────────────
    total_neg = sum(len(negative[s]) for s in sources)
    total_orig = sum(len(all_reviews.get(s, [])) for s in sources)

    negative["summary"] = {
        **all_reviews.get("summary", {}),
        "filter_applied": "negative_only",
        "sentiment_model": sf.model_type,
        "original_total": total_orig,
        "negative_total": total_neg,
        "filtered_out": total_orig - total_neg,
        "google_play_negative": len(negative["google_play"]),
        "app_store_negative": len(negative["app_store"]),
        "reddit_negative": len(negative["reddit"]),
    }

    print(f"\n[Sentiment Filter] Done — {total_neg}/{total_orig} reviews are negative")
    return negative


# ══════════════════════════════════════════════════════════
# GOOGLE PLAY STORE
# ══════════════════════════════════════════════════════════

def _get_play_package_id(app_name: str) -> tuple[str, str] | tuple[None, None]:
    """Scrape Google Play search page to get the real package ID."""
    query = urllib.parse.quote(app_name)
    url = f"https://play.google.com/store/search?q={query}&c=apps&hl=en&gl=us"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }
    resp = requests.get(url, headers=headers, timeout=10)
    resp.raise_for_status()

    package_ids = re.findall(r'/store/apps/details\?id=([\w.]+)', resp.text)
    seen = set()
    unique_ids = [p for p in package_ids if not (p in seen or seen.add(p))]

    if not unique_ids:
        return None, None

    package_id = unique_ids[0]
    title_match = re.search(
        r'aria-label="([^"]+)"[^>]*href="/store/apps/details\?id=' + re.escape(package_id),
        resp.text,
    )
    title = title_match.group(1) if title_match else app_name
    return package_id, title


def fetch_google_play_reviews(app_name: str, count: int = 20) -> list[dict]:
    try:
        from google_play_scraper import reviews, Sort

        print(f"\n[Google Play] Searching for '{app_name}'...")
        app_id, app_title = _get_play_package_id(app_name)

        if not app_id:
            print("[Google Play] Could not find app on Google Play.")
            return []

        print(f"[Google Play] Found: {app_title} ({app_id})")

        review_results, _ = reviews(
            app_id,
            lang="en",
            country="us",
            sort=Sort.MOST_RELEVANT,
            count=count,
        )

        formatted = []
        for r in review_results:
            date_val = r.get("at", "")
            if hasattr(date_val, "strftime"):
                date_val = date_val.strftime("%Y-%m-%d")
            formatted.append({
                "source": "Google Play",
                "app": app_title,
                "author": r.get("userName", "Anonymous"),
                "rating": r.get("score"),
                "title": r.get("title", ""),
                "content": r.get("content", ""),
                "date": date_val,
                "thumbs_up": r.get("thumbsUpCount", 0),
            })

        print(f"[Google Play] Fetched {len(formatted)} reviews.")
        return formatted

    except ImportError:
        print("[Google Play] ERROR: Run 'pip install google-play-scraper'")
        return []
    except Exception as e:
        print(f"[Google Play] ERROR: {e}")
        return []


# ══════════════════════════════════════════════════════════
# APPLE APP STORE
# ══════════════════════════════════════════════════════════

def fetch_appstore_reviews(app_name: str, count: int = 20, country: str = "us") -> list[dict]:
    try:
        print(f"\n[App Store] Searching for '{app_name}'...")
        search_url = "https://itunes.apple.com/search"
        params = {"term": app_name, "country": country, "entity": "software", "limit": 5}
        resp = requests.get(search_url, params=params, timeout=10)
        resp.raise_for_status()
        itunes_results = resp.json().get("results", [])

        if not itunes_results:
            print("[App Store] No apps found.")
            return []

        app = itunes_results[0]
        app_id = app["trackId"]
        app_title = app["trackName"]
        print(f"[App Store] Found: {app_title} (ID: {app_id})")

        formatted = []
        pages_needed = min(10, -(-count // 50))

        for page in range(1, pages_needed + 1):
            rss_url = (
                f"https://itunes.apple.com/{country}/rss/customerreviews/"
                f"page={page}/id={app_id}/sortby=mostrecent/json"
            )
            r = requests.get(rss_url, timeout=10)
            if r.status_code != 200:
                break

            data = r.json()
            entries = data.get("feed", {}).get("entry", [])
            if page == 1 and entries:
                entries = entries[1:]
            if not entries:
                break

            for entry in entries:
                formatted.append({
                    "source": "App Store",
                    "app": app_title,
                    "author": entry.get("author", {}).get("name", {}).get("label", "Anonymous"),
                    "rating": int(entry.get("im:rating", {}).get("label", 0)),
                    "title": entry.get("title", {}).get("label", ""),
                    "content": entry.get("content", {}).get("label", ""),
                    "date": entry.get("updated", {}).get("label", "")[:10],
                    "thumbs_up": entry.get("im:voteCount", {}).get("label", 0),
                })
                if len(formatted) >= count:
                    break

            if len(formatted) >= count:
                break
            time.sleep(0.5)

        print(f"[App Store] Fetched {len(formatted)} reviews.")
        return formatted

    except Exception as e:
        print(f"[App Store] ERROR: {e}")
        return []


# ══════════════════════════════════════════════════════════
# REDDIT
# ══════════════════════════════════════════════════════════

def fetch_reddit_reviews(product_name: str, count: int = 20) -> list[dict]:
    print(f"\n[Reddit] Searching for '{product_name}' reviews...")
    try:
        headers = {"User-Agent": "ReviewFetcher/1.0"}
        params = {
            "q": f"{product_name} review",
            "sort": "relevance",
            "limit": count,
            "type": "link",
        }
        url = "https://www.reddit.com/search.json"
        resp = requests.get(url, headers=headers, params=params, timeout=10)
        resp.raise_for_status()
        posts = resp.json()["data"]["children"]

        formatted = []
        for post in posts:
            d = post["data"]
            post_date = datetime.fromtimestamp(
                d.get("created_utc", 0), tz=timezone.utc
            ).strftime("%Y-%m-%d")
            formatted.append({
                "source": "Reddit",
                "app": product_name,
                "author": d.get("author", ""),
                "rating": None,
                "title": d.get("title", ""),
                "content": d.get("selftext", "")[:500] or "[Link post]",
                "date": post_date,
                "upvotes": d.get("score", 0),
                "url": f"https://reddit.com{d.get('permalink', '')}",
                "subreddit": d.get("subreddit", ""),
            })

        print(f"[Reddit] Fetched {len(formatted)} posts.")
        return formatted

    except Exception as e:
        print(f"[Reddit] ERROR: {e}")
        return []


# ══════════════════════════════════════════════════════════
# MAIN AGGREGATOR
# ══════════════════════════════════════════════════════════

def fetch_all_reviews(
    product_name: str,
    count_per_source: int = 20,
    sources: list[str] = None,
) -> dict:
    if sources is None:
        sources = ["google_play", "app_store", "reddit"]

    all_reviews = {}

    if "google_play" in sources:
        all_reviews["google_play"] = fetch_google_play_reviews(product_name, count_per_source)
        time.sleep(1)

    if "app_store" in sources:
        all_reviews["app_store"] = fetch_appstore_reviews(product_name, count_per_source)
        time.sleep(1)

    if "reddit" in sources:
        all_reviews["reddit"] = fetch_reddit_reviews(product_name, count_per_source)

    total = sum(len(v) for v in all_reviews.values())
    gp_reviews = all_reviews.get("google_play", [])
    as_reviews = all_reviews.get("app_store", [])

    gp_avg = (
        round(sum(r["rating"] for r in gp_reviews if r.get("rating")) / len(gp_reviews), 2)
        if gp_reviews else None
    )
    as_avg = (
        round(sum(r["rating"] for r in as_reviews if r.get("rating")) / len(as_reviews), 2)
        if as_reviews else None
    )

    all_reviews["summary"] = {
        "product": product_name,
        "fetched_at": datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "total_reviews": total,
        "google_play_count": len(gp_reviews),
        "google_play_avg_rating": gp_avg,
        "app_store_count": len(as_reviews),
        "app_store_avg_rating": as_avg,
        "reddit_posts_count": len(all_reviews.get("reddit", [])),
    }

    print("\n" + "=" * 50)
    print(f"  SUMMARY FOR: {product_name.upper()}")
    print("=" * 50)
    print(f"  Google Play  → {all_reviews['summary']['google_play_count']} reviews"
          + (f" | Avg rating: {gp_avg}/5" if gp_avg else ""))
    print(f"  App Store    → {all_reviews['summary']['app_store_count']} reviews"
          + (f" | Avg rating: {as_avg}/5" if as_avg else ""))
    print(f"  Reddit       → {all_reviews['summary']['reddit_posts_count']} posts")
    print(f"  TOTAL        → {total} items fetched")
    print("=" * 50)

    return all_reviews


# ══════════════════════════════════════════════════════════
# CLI ENTRY POINT
# ══════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("╔════════════════════════════════════════════════╗")
    print("║   Multi-Platform Review Fetcher                ║")
    print("║   Google Play | App Store | Reddit             ║")
    print("║   Negative Reviews Only (DistilBERT/VADER)     ║")
    print("╚════════════════════════════════════════════════╝\n")

    product = input("Enter product/app name: ").strip()
    if not product:
        print("No product name entered. Exiting.")
        exit(1)

    count_input = input("Reviews per platform (default 20): ").strip()
    count = int(count_input) if count_input.isdigit() else 20

    print("\nWhich sources? (leave blank for all)")
    print("  Options: google_play, app_store, reddit")
    sources_input = input("Sources (comma-separated, or Enter for all): ").strip()
    sources = (
        [s.strip() for s in sources_input.split(",") if s.strip()]
        if sources_input else None
    )

    # ── Step 1: Fetch all reviews ─────────────────────────────────────────
    all_reviews = fetch_all_reviews(product, count_per_source=count, sources=sources)

    # ── Step 2: Always filter to negative only ────────────────────────────
    negative_reviews = filter_negative_reviews(
        all_reviews,
        model="auto",
        threshold=0.6,
        add_sentiment_scores=True,
    )

    # ── Step 3: Print summary ─────────────────────────────────────────────
    s = negative_reviews["summary"]
    print("\n" + "=" * 50)
    print(f"  NEGATIVE REVIEWS FOR: {product.upper()}")
    print("=" * 50)
    print(f"  Google Play  → {s['google_play_negative']} negative")
    print(f"  App Store    → {s['app_store_negative']} negative")
    print(f"  Reddit       → {s['reddit_negative']} negative")
    print(f"  TOTAL        → {s['negative_total']} / {s['original_total']} reviews")
    print(f"  Model used   → {s['sentiment_model']}")
    print("=" * 50)

    # ── Step 4: Save to file ──────────────────────────────────────────────
    filename = f"{product.lower().replace(' ', '_')}_negative_reviews.json"
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(negative_reviews, f, indent=2, ensure_ascii=False, default=str)
    print(f"\n  Saved to: {filename}")

    # ── Step 5: Print samples ─────────────────────────────────────────────
    show = input("\nPrint sample negative reviews? (y/n): ").strip().lower()
    if show == "y":
        for source in ["google_play", "app_store", "reddit"]:
            rev_list = negative_reviews.get(source, [])
            if not rev_list:
                continue
            print(f"\n── {source.upper()} NEGATIVE SAMPLES ──")
            for r in rev_list[:3]:
                stars = f"{r['rating']}★" if r.get("rating") else "N/A"
                conf = f" [conf: {r['sentiment']['score']}]" if "sentiment" in r else ""
                print(f"  [{stars}]{conf} {r.get('title', '')} — {r.get('author', '')}")
                print(f"  {r.get('content', '')[:250]}")
                print()