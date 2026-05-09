#!/usr/bin/env python3
"""
Google Alerts Daily Digest - Fetch and Summarize Script
Runs daily via GitHub Actions, outputs JSON to docs/data/YYYY-MM-DD.json
"""

import os
import sys
import json
import hashlib
import re
import time
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import feedparser
import yaml
from google import genai
from google.genai import types

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))
REPO_ROOT = Path(__file__).parent.parent


def load_config() -> dict:
    with open(REPO_ROOT / "config.yml") as f:
        return yaml.safe_load(f)


def load_feeds(feeds_file: Path) -> list[dict]:
    if not feeds_file.exists():
        log.warning(f"feeds.json not found at {feeds_file}, no feeds to process")
        return []
    with open(feeds_file) as f:
        data = json.load(f)
    return data.get("feeds", [])


# ── State management ──────────────────────────────────────────────────────────

def article_id(url: str) -> str:
    return hashlib.md5(url.strip().encode()).hexdigest()


def load_processed_ids(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        log.warning("Could not read processed_ids file, starting fresh")
        return {}


def save_processed_ids(path: Path, processed: dict) -> None:
    cutoff = datetime.now(JST) - timedelta(days=30)
    pruned = {
        aid: ts for aid, ts in processed.items()
        if datetime.fromisoformat(ts) >= cutoff
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(pruned, f, ensure_ascii=False)
    log.info(f"Saved {len(pruned)} processed IDs (pruned {len(processed) - len(pruned)} old)")


# ── RSS fetching ───────────────────────────────────────────────────────────────

def parse_entry_date(entry) -> Optional[datetime]:
    for attr in ("published_parsed", "updated_parsed"):
        t = getattr(entry, attr, None)
        if t:
            return datetime(*t[:6], tzinfo=timezone.utc)
    return None


def fetch_feed(feed_cfg: dict, lookback_hours: int, max_articles: int,
               processed_ids: dict) -> list[dict]:
    url = feed_cfg["url"]
    keyword = feed_cfg.get("keyword", "")
    category = feed_cfg.get("category", "その他")
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)

    log.info(f"Fetching feed: {keyword} ({url[:60]}...)")
    try:
        parsed = feedparser.parse(url)
    except Exception as e:
        log.error(f"Failed to fetch {url}: {e}")
        return []

    if parsed.bozo and not parsed.entries:
        log.warning(f"Feed parse error for {keyword}: {parsed.bozo_exception}")
        return []

    articles = []
    for entry in parsed.entries:
        pub_date = parse_entry_date(entry)
        if pub_date and pub_date < cutoff:
            continue

        link = entry.get("link", "")
        if not link:
            continue

        aid = article_id(link)
        if aid in processed_ids:
            continue

        raw_snippet = entry.get("summary", entry.get("title", ""))
        snippet = re.sub(r"<[^>]+>", " ", raw_snippet).strip()[:500]

        articles.append({
            "id": aid,
            "title": entry.get("title", "").strip(),
            "url": link,
            "source": entry.get("source", {}).get("title", parsed.feed.get("title", "")),
            "published_iso": pub_date.astimezone(JST).isoformat() if pub_date else None,
            "snippet": snippet,
            "keyword": keyword,
            "category": category,
        })

        if len(articles) >= max_articles:
            break

    log.info(f"  -> {len(articles)} new articles from {keyword}")
    return articles


# ── Gemini summarization ──────────────────────────────────────────────────────

SUMMARY_PROMPT = """\
以下のニュース記事を読み、日本語で2〜3文の要約を書いてください。

記事タイトル: {title}
記事の抜粋: {snippet}

要件:
- 必ず日本語で回答する
- 元の言語が英語などの場合は日本語に翻訳して要約する
- 重要な情報（誰が・何を・なぜ）を簡潔にまとめる
- 2〜3文で完結させる
- 余分な説明や前置きは不要

要約:"""


class RateLimiter:
    def __init__(self, rpm: int):
        self.min_interval = 60.0 / rpm
        self._last_call = 0.0

    def wait(self):
        now = time.monotonic()
        elapsed = now - self._last_call
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_call = time.monotonic()


def summarize_article(client: genai.Client, model: str, article: dict,
                      rate_limiter: RateLimiter, daily_counter: list) -> Optional[str]:
    rate_limiter.wait()

    prompt = SUMMARY_PROMPT.format(
        title=article["title"],
        snippet=article["snippet"] or article["title"],
    )

    for attempt in range(3):
        try:
            response = client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    max_output_tokens=256,
                    temperature=0.3,
                ),
            )
            daily_counter[0] += 1
            return response.text.strip()

        except Exception as e:
            err_str = str(e).lower()
            if "429" in err_str or "quota" in err_str or "resource_exhausted" in err_str:
                if attempt < 2:
                    wait = 60 * (attempt + 1)
                    log.warning(f"Rate limit hit, waiting {wait}s (attempt {attempt + 1})")
                    time.sleep(wait)
                else:
                    log.error("Rate limit: giving up on article after 3 attempts")
                    return None
            elif any(code in err_str for code in ["500", "503", "unavailable"]):
                if attempt < 2:
                    time.sleep(10 * (attempt + 1))
                else:
                    log.error("Server error: giving up on article")
                    return None
            else:
                log.error(f"Gemini API error: {e}")
                return None

    return None


# ── Output ─────────────────────────────────────────────────────────────────────

def load_or_create_daily_file(path: Path) -> dict:
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return {
        "date": path.stem,
        "generated_at": datetime.now(JST).isoformat(),
        "article_count": 0,
        "articles": [],
    }


def save_daily_file(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data["article_count"] = len(data["articles"])
    data["generated_at"] = datetime.now(JST).isoformat()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    log.info(f"Wrote {data['article_count']} articles to {path}")


def write_date_index(data_dir: Path) -> None:
    dates = sorted(
        [p.stem for p in data_dir.glob("????-??-??.json")],
        reverse=True,
    )
    index_path = data_dir / "index.json"
    with open(index_path, "w") as f:
        json.dump({"dates": dates, "updated_at": datetime.now(JST).isoformat()}, f)
    log.info(f"Updated date index: {len(dates)} dates available")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    config = load_config()
    settings = config["settings"]

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        log.error("GEMINI_API_KEY environment variable not set")
        sys.exit(1)

    feeds_path = REPO_ROOT / settings["feeds_file"]
    feeds = load_feeds(feeds_path)
    if not feeds:
        log.info("No feeds configured. Add feeds via the admin page and re-run.")
        sys.exit(0)

    client = genai.Client(api_key=api_key)
    model = settings["gemini_model"]
    rate_limiter = RateLimiter(rpm=settings["gemini_rpm_limit"])
    daily_counter = [0]

    processed_ids_path = REPO_ROOT / settings["processed_ids_file"]
    processed_ids = load_processed_ids(processed_ids_path)
    log.info(f"Loaded {len(processed_ids)} previously processed article IDs")

    today_str = datetime.now(JST).strftime("%Y-%m-%d")
    output_path = REPO_ROOT / settings["output_dir"] / f"{today_str}.json"
    daily_data = load_or_create_daily_file(output_path)

    existing_ids = {a["id"] for a in daily_data["articles"]}
    new_processed = {}
    total_new = 0

    for feed_cfg in feeds:
        if daily_counter[0] >= settings["gemini_rpd_limit"] - 10:
            log.warning("Approaching daily API quota limit, stopping early")
            break

        articles = fetch_feed(
            feed_cfg,
            lookback_hours=settings["lookback_hours"],
            max_articles=settings["max_articles_per_feed"],
            processed_ids={**processed_ids, **new_processed},
        )

        for article in articles:
            if article["id"] in existing_ids:
                continue

            if daily_counter[0] >= settings["gemini_rpd_limit"] - 10:
                log.warning("Daily quota limit reached mid-feed")
                break

            summary_ja = summarize_article(client, model, article, rate_limiter, daily_counter)

            article["summary_ja"] = summary_ja or "(要約を生成できませんでした)"
            article["summarized"] = summary_ja is not None

            daily_data["articles"].append(article)
            existing_ids.add(article["id"])
            new_processed[article["id"]] = datetime.now(JST).isoformat()
            total_new += 1

            log.info(f"Summarized: {article['title'][:60]}...")

    save_daily_file(output_path, daily_data)

    processed_ids.update(new_processed)
    save_processed_ids(processed_ids_path, processed_ids)

    log.info(f"Done. {total_new} new articles processed. {daily_counter[0]} Gemini API calls made.")

    write_date_index(REPO_ROOT / settings["output_dir"])


if __name__ == "__main__":
    main()
