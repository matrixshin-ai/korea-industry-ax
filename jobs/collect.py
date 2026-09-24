"""
RSS + Naver News Search collection -> candidates.json

- RSS: config/sources.yaml `feeds` list (press-only, no public-agency feeds).
- Naver: config/queries.yaml `queries` list, paginated per query until the
  window boundary is reached or the 1,000-result API cap is hit.
- Writes a per-run log to logs/run_YYYYMMDD_HHMM.json with per-feed and
  per-query counts, so failures are visible without re-running.
"""
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape

import feedparser
import yaml
from dateutil import parser as dtparser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from timewindow import KST, get_collection_hours, within_window

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCES_PATH = os.path.join(ROOT, "config", "sources.yaml")
QUERIES_PATH = os.path.join(ROOT, "config", "queries.yaml")
CANDIDATES_PATH = os.path.join(ROOT, "candidates.json")
LOGS_DIR = os.path.join(ROOT, "logs")

MIN_TITLE_LEN = 12
NAVER_MAX_TOTAL = 1000
NAVER_PAGE_SIZE = 100
FEED_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) korea-industry-ax-collector/1.0"


def load_sources():
    with open(SOURCES_PATH, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get("feeds", []), data.get("domain_map", {})


def load_queries():
    with open(QUERIES_PATH, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    queries = []
    for group in data.get("categories", {}).values():
        queries.extend(group)
    return queries


def should_drop_item(title: str, summary: str) -> bool:
    t = (title or "").strip()
    s = (summary or "").strip()
    return (s == "") and (len(t) < MIN_TITLE_LEN)


def parse_rss_published(entry):
    for key in ["published", "updated", "pubDate"]:
        val = getattr(entry, key, None)
        if not val:
            continue
        try:
            return dtparser.parse(val)
        except (ValueError, TypeError, OverflowError):
            pass
        # Some feeds (fnnews, sedaily) omit the space after the weekday comma
        # ("Thu,24 Sep 2026 ..."), which trips dateutil's parser.
        try:
            return dtparser.parse(re.sub(r",(\S)", r", \1", val))
        except (ValueError, TypeError, OverflowError):
            pass
    return None


def fetch_rss_candidates(feeds, hours):
    seen = set()
    candidates = []
    feed_log = []

    print(f"Processing {len(feeds)} RSS feeds...")

    for feed in feeds:
        url = feed["url"]
        source = feed["source"]
        entry_count = 0
        window_count = 0
        error = None
        try:
            d = feedparser.parse(url, agent=FEED_USER_AGENT)
            if d.bozo and not d.entries:
                error = f"parse failed: {getattr(d, 'bozo_exception', 'unknown error')}"
                feed_log.append({"source": source, "url": url, "status": "fail", "error": error})
                continue

            for e in d.entries:
                entry_count += 1
                link = getattr(e, "link", None)
                if not link or link in seen:
                    continue

                published = parse_rss_published(e)
                if not within_window(published, hours):
                    continue

                title = getattr(e, "title", "").strip()
                summary = getattr(e, "summary", "").strip()
                if should_drop_item(title, summary):
                    continue

                seen.add(link)
                window_count += 1
                if len(summary) > 500:
                    summary = summary[:500] + "..."

                candidates.append({
                    "title": title,
                    "summary": summary,
                    "link": link,
                    "source": source,
                    "published": published.isoformat() if published else None,
                    "collected_via": "rss",
                    "origin_feed": url,
                })

            feed_log.append({
                "source": source, "url": url, "status": "ok",
                "entries": entry_count, "within_window": window_count,
            })
            print(f"  OK   {source}: {window_count}/{entry_count} within window")
        except Exception as ex:  # noqa: BLE001 - one bad feed must not stop the run
            error = str(ex)
            feed_log.append({"source": source, "url": url, "status": "fail", "error": error})
            print(f"  FAIL {source}: {error}")

    return candidates, feed_log


def clean_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text or "")
    return unescape(text).strip()


def source_from_domain(link: str, domain_map: dict) -> str:
    try:
        netloc = urllib.parse.urlparse(link).netloc.lower()
    except ValueError:
        return link
    netloc = netloc[4:] if netloc.startswith("www.") else netloc
    for domain, name in domain_map.items():
        if netloc == domain or netloc.endswith("." + domain):
            return name
    return netloc


def parse_naver_date(date_str: str):
    try:
        return parsedate_to_datetime(date_str)
    except (ValueError, TypeError):
        return None


def search_news_page(query: str, start: int, client_id: str, client_secret: str):
    encoded_query = urllib.parse.quote(query)
    url = (
        f"https://openapi.naver.com/v1/search/news.json"
        f"?query={encoded_query}&display={NAVER_PAGE_SIZE}&start={start}&sort=date"
    )
    request = urllib.request.Request(url)
    request.add_header("X-Naver-Client-Id", client_id)
    request.add_header("X-Naver-Client-Secret", client_secret)
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_naver_candidates(queries, hours, domain_map):
    client_id = os.environ.get("NAVER_CLIENT_ID", "")
    client_secret = os.environ.get("NAVER_CLIENT_SECRET", "")

    if not client_id or not client_secret:
        print("  Naver API credentials not configured - skipping Naver search")
        return [], [], False

    seen_links = set()
    candidates = []
    query_log = []
    print(f"  Searching Naver with {len(queries)} queries...")

    for query in queries:
        start = 1
        calls = 0
        results_in_window = 0
        saturated = False
        stop = False
        error = None

        while not stop:
            try:
                data = search_news_page(query, start, client_id, client_secret)
                calls += 1
            except urllib.error.HTTPError as e:
                error = f"HTTP {e.code}"
                break
            except Exception as e:  # noqa: BLE001 - one bad query must not stop the run
                error = str(e)
                break

            items = data.get("items", [])
            if not items:
                break

            oldest_in_page_within_window = False
            for item in items:
                original_link = item.get("originallink", "")
                naver_link = item.get("link", "")
                link = original_link if original_link else naver_link
                if not link:
                    continue

                pub_date = parse_naver_date(item.get("pubDate", ""))
                if not within_window(pub_date, hours):
                    continue
                oldest_in_page_within_window = True

                if link in seen_links:
                    continue
                seen_links.add(link)

                title = clean_html(item.get("title", ""))
                summary = clean_html(item.get("description", ""))
                if len(summary) > 500:
                    summary = summary[:500] + "..."

                candidates.append({
                    "title": title,
                    "summary": summary,
                    "link": link,
                    "source": source_from_domain(link, domain_map),
                    "published": pub_date.isoformat() if pub_date else None,
                    "collected_via": "naver",
                    "origin_query": query,
                })
                results_in_window += 1

            start += NAVER_PAGE_SIZE
            if start > NAVER_MAX_TOTAL:
                saturated = True
                stop = True
            elif len(items) < NAVER_PAGE_SIZE:
                stop = True
            elif not oldest_in_page_within_window:
                stop = True

        if error:
            query_log.append({"query": query, "calls": calls, "error": error})
        else:
            query_log.append({
                "query": query, "calls": calls,
                "results_in_window": results_in_window, "saturated": saturated,
            })
            if saturated:
                print(f"  WARN saturated (1000-result cap hit): {query}")

    total_calls = sum(q["calls"] for q in query_log)
    print(f"  Naver: {len(candidates)} unique candidates, {total_calls} calls")
    return candidates, query_log, True


def main():
    print(f"\n{'=' * 60}")
    print(f"Collection started at {datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S KST')}")
    print(f"{'=' * 60}\n")

    hours = get_collection_hours()
    print(f"Collection window: {hours}h ({'Monday' if hours == 48 else 'Tue-Sat'})\n")

    feeds, domain_map = load_sources()
    queries = load_queries()

    rss_candidates, feed_log = fetch_rss_candidates(feeds, hours)
    naver_candidates, query_log, naver_attempted = fetch_naver_candidates(queries, hours, domain_map)

    seen_links = set()
    combined = []
    for c in rss_candidates + naver_candidates:
        link = c.get("link", "")
        if link and link not in seen_links:
            seen_links.add(link)
            combined.append(c)

    with open(CANDIDATES_PATH, "w", encoding="utf-8") as f:
        json.dump(combined, f, ensure_ascii=False, indent=2)

    os.makedirs(LOGS_DIR, exist_ok=True)
    run_log = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "collection_hours": hours,
        "rss": {
            "feed_count": len(feeds),
            "candidates": len(rss_candidates),
            "feeds": feed_log,
        },
        "naver": {
            "attempted": naver_attempted,
            "query_count": len(queries),
            "candidates": len(naver_candidates),
            "total_calls": sum(q.get("calls", 0) for q in query_log),
            "saturated_queries": [q["query"] for q in query_log if q.get("saturated")],
            "queries": query_log,
        },
        "combined_candidates": len(combined),
    }
    log_path = os.path.join(LOGS_DIR, f"run_{datetime.now(KST).strftime('%Y%m%d_%H%M')}.json")
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(run_log, f, ensure_ascii=False, indent=2)

    print(f"\n{'=' * 60}")
    print(f"Collection complete: {len(combined)} candidates ({len(rss_candidates)} RSS + {len(naver_candidates)} Naver, deduped)")
    print(f"Log: {log_path}")
    print(f"{'=' * 60}\n")


if __name__ == "__main__":
    main()
