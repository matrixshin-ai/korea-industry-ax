"""
classified.json -> public/data.json

1. Use only this run's fresh candidates (no carry-over from a previous day).
2. Drop exact-duplicate URLs (after normalization).
3. Drop relevant != true (includes unclassified items - they retry next run).
4. Merge same-event duplicates via dedup.py; representative gets `related`.
5. Apply the KST collection window (same helper collect.py used).
6. Sort within each section: score = ax + ulsan_score + core*10, desc, then newest first.
7. Write public/data.json with generation stats for the footer/status line.
"""
import glob
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dedup import deduplicate_articles
from timewindow import KST, get_collection_hours, within_window
from urlnorm import normalize_url

from dateutil import parser as dtparser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLASSIFIED_PATH = os.path.join(ROOT, "classified.json")
DATA_PATH = os.path.join(ROOT, "public", "data.json")
LOGS_DIR = os.path.join(ROOT, "logs")

SECTION_LABELS = {1: "기업·현장", 2: "기술·인프라", 3: "정책·생태계·인재"}


def load_latest_run_log():
    pattern = os.path.join(LOGS_DIR, "run_*.json")
    files = glob.glob(pattern)
    if not files:
        return None
    latest = max(files, key=os.path.getmtime)
    with open(latest, "r", encoding="utf-8") as f:
        return json.load(f)


def parse_published(value):
    if not value:
        return None
    try:
        return dtparser.parse(value)
    except (ValueError, TypeError, OverflowError):
        return None


def main():
    if not os.path.exists(CLASSIFIED_PATH):
        print(f"No classified.json at {CLASSIFIED_PATH} - nothing to build")
        sys.exit(1)

    with open(CLASSIFIED_PATH, "r", encoding="utf-8") as f:
        classified = json.load(f)

    hours = get_collection_hours()

    relevant = [c for c in classified if c.get("relevant") is True and c.get("section") in (1, 2, 3)]
    dropped_not_relevant = len(classified) - len(relevant)

    seen_urls = set()
    exact_deduped = []
    for c in relevant:
        key = normalize_url(c.get("link", ""))
        if key and key not in seen_urls:
            seen_urls.add(key)
            exact_deduped.append(c)
    dropped_exact_dupe = len(relevant) - len(exact_deduped)

    windowed = []
    for c in exact_deduped:
        published = parse_published(c.get("published"))
        if within_window(published, hours):
            windowed.append(c)
    dropped_out_of_window = len(exact_deduped) - len(windowed)

    before_semantic_dedup = len(windowed)
    deduped = deduplicate_articles(windowed)
    after_semantic_dedup = len(deduped)

    sections = {"1": [], "2": [], "3": []}
    for item in deduped:
        published = parse_published(item.get("published"))
        score = item.get("ax", 0) + item.get("ulsan_score", 0) + item.get("core", 0) * 10
        item["score"] = score
        item["_sort_key"] = (score, published or datetime.min.replace(tzinfo=timezone.utc))
        sections[str(item["section"])].append(item)

    for key in sections:
        sections[key].sort(
            key=lambda it: (it["_sort_key"][0], it["_sort_key"][1].isoformat() if it["_sort_key"][1] else ""),
            reverse=True,
        )
        for it in sections[key]:
            it.pop("_sort_key", None)
            it.pop("id", None)
            it.pop("collected_via", None)
            it.pop("unclassified", None)

    run_log = load_latest_run_log() or {}

    data = {
        "generated_at": datetime.now(KST).isoformat(),
        "collection_hours": hours,
        "article_count": sum(len(v) for v in sections.values()),
        "stats": {
            "classified_total": len(classified),
            "dropped_not_relevant": dropped_not_relevant,
            "dropped_exact_duplicate": dropped_exact_dupe,
            "dropped_out_of_window": dropped_out_of_window,
            "before_semantic_dedup": before_semantic_dedup,
            "after_semantic_dedup": after_semantic_dedup,
            "rss": run_log.get("rss", {}),
            "naver": run_log.get("naver", {}),
        },
        "sections": {
            "1": {"label": SECTION_LABELS[1], "items": sections["1"]},
            "2": {"label": SECTION_LABELS[2], "items": sections["2"]},
            "3": {"label": SECTION_LABELS[3], "items": sections["3"]},
        },
    }

    os.makedirs(os.path.dirname(DATA_PATH), exist_ok=True)
    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"Built public/data.json: {data['article_count']} articles "
          f"(section1={len(sections['1'])}, section2={len(sections['2'])}, section3={len(sections['3'])})")
    print(f"  relevant-only: {len(relevant)}, exact-dedup: -{dropped_exact_dupe}, "
          f"window-filtered: -{dropped_out_of_window}, semantic-dedup: {before_semantic_dedup} -> {after_semantic_dedup}")


if __name__ == "__main__":
    main()
