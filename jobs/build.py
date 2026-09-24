"""
classified.json -> public/data.json

1. Use only this run's fresh candidates (no carry-over from a previous day),
   keeping every graded item (S/A/B/C/X) at this stage - not just S/A/B - so
   a same-event duplicate that individually landed on C or X can still ride
   along as a `related` citation under a higher-graded representative.
2. Drop exact-duplicate URLs (after normalization).
3. Apply the KST collection window (same helper collect.py used).
4. Merge same-event duplicates: dedup.py's algorithmic pass first, then a
   small targeted Haiku pass (jobs/llm_dedup.py) on whatever still shares a
   detected organization name afterward.
5. Event-level grade unification: for each merged group, the group's grade/
   section/etc become whichever single member graded highest (S > A > B >
   C > X); every other member - regardless of its own grade - becomes a
   `related` citation under that representative.
6. Keep only groups whose representative graded S/A/B (C/X groups are
   dropped entirely; an unclassified item just retries whenever it's
   re-collected).
7. Sort within each section: score = grade_points(S=100/A=70/B=40) +
   ulsan_score + core*10, desc, then newest first.
8. Write public/data.json with generation stats for the footer/status line.
"""
import glob
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from classify import GRADE_POINTS, GRADES
from dedup import deduplicate_articles
import llm_dedup
from timewindow import KST, get_collection_hours, within_window
from urlnorm import normalize_url

from dateutil import parser as dtparser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLASSIFIED_PATH = os.path.join(ROOT, "classified.json")
DATA_PATH = os.path.join(ROOT, "public", "data.json")
LOGS_DIR = os.path.join(ROOT, "logs")

SECTION_LABELS = {1: "기업·현장", 2: "기술·인프라", 3: "정책·생태계·인재"}
PUBLISHED_GRADES = ("S", "A", "B")
GRADE_ORDER = {"S": 5, "A": 4, "B": 3, "C": 2, "X": 1}


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


def _all_members(item):
    """Flatten item + any nested `related` members (from either dedup pass)
    into one flat list - a merged group can be nested one or two levels deep
    depending on which pass(es) touched it."""
    members = [item]
    for rel in item.get("related", []) or []:
        members.extend(_all_members(rel))
    return members


def _group_has_publishable_grade(item):
    return any(m.get("grade") in PUBLISHED_GRADES for m in _all_members(item))


def regrade_representatives(items):
    """Requirement 4: unify each merged group onto its single highest-graded
    member's grade/section/etc. Every other member - whatever its own grade -
    becomes a `related` citation under that representative."""
    result = []
    for item in items:
        members = _all_members(item)
        best = max(members, key=lambda m: GRADE_ORDER.get(m.get("grade"), 0))
        seen_links = set()
        others = []
        for m in members:
            if m is best:
                continue
            link = m.get("link", "")
            if not link or link in seen_links:
                continue
            seen_links.add(link)
            others.append({"source": m.get("source", ""), "link": link})
        new_item = {k: v for k, v in best.items() if k != "related"}
        new_item["related"] = others
        result.append(new_item)
    return result


def main():
    if not os.path.exists(CLASSIFIED_PATH):
        print(f"No classified.json at {CLASSIFIED_PATH} - nothing to build")
        sys.exit(1)

    with open(CLASSIFIED_PATH, "r", encoding="utf-8") as f:
        classified = json.load(f)

    hours = get_collection_hours()

    graded = [c for c in classified if c.get("grade") in GRADES]
    dropped_unclassified = len(classified) - len(graded)

    seen_urls = set()
    exact_deduped = []
    for c in graded:
        key = normalize_url(c.get("link", ""))
        if key and key not in seen_urls:
            seen_urls.add(key)
            exact_deduped.append(c)
    dropped_exact_dupe = len(graded) - len(exact_deduped)

    windowed = []
    for c in exact_deduped:
        pub_dt = parse_published(c.get("published"))
        if within_window(pub_dt, hours):
            windowed.append(c)
    dropped_out_of_window = len(exact_deduped) - len(windowed)

    before_algo_dedup = len(windowed)
    deduped = deduplicate_articles(windowed)
    after_algo_dedup = len(deduped)

    llm_dedup_log = {}
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    all_clusters = llm_dedup.build_clusters(deduped)
    # Cost cut: a cluster made entirely of C/X members will never get published
    # regardless of how it's merged, so there's no point spending a Haiku call
    # deciding how to group it.
    clusters = [c for c in all_clusters if any(_group_has_publishable_grade(it) for it in c)]
    llm_dedup_log["suspect_clusters"] = len(clusters)
    llm_dedup_log["suspect_clusters_skipped_no_publishable_grade"] = len(all_clusters) - len(clusters)
    llm_dedup_log["suspect_items"] = sum(len(c) for c in clusters)
    if clusters and api_key:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        merge_map = llm_dedup.llm_merge_clusters(client, clusters, llm_dedup_log)
        deduped = llm_dedup.apply_merge_map(deduped, merge_map)
    elif clusters:
        print(f"  ANTHROPIC_API_KEY not set - skipping LLM dedup pass on {len(clusters)} suspect clusters")
    after_llm_dedup = len(deduped)

    regraded = regrade_representatives(deduped)
    published = [item for item in regraded if item.get("grade") in PUBLISHED_GRADES]
    dropped_not_published = len(regraded) - len(published)

    sections = {"1": [], "2": [], "3": []}
    dropped_invalid_section = 0
    for item in published:
        if str(item.get("section")) not in sections:
            # Defensive: classify.py validates `section` before ever writing a
            # non-X grade, so this shouldn't happen - but a malformed Haiku
            # response is cheaper to skip here than to crash the whole build on.
            dropped_invalid_section += 1
            continue
        pub_dt = parse_published(item.get("published"))
        score = GRADE_POINTS.get(item.get("grade"), 0) + item.get("ulsan_score", 0) + item.get("core", 0) * 10
        item["score"] = score
        item["_sort_key"] = (score, pub_dt or datetime.min.replace(tzinfo=timezone.utc))
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
            it.pop("prefiltered", None)

    run_log = load_latest_run_log() or {}

    grade_counts = {"S": 0, "A": 0, "B": 0}
    for item in published:
        grade_counts[item.get("grade")] = grade_counts.get(item.get("grade"), 0) + 1

    data = {
        "generated_at": datetime.now(KST).isoformat(),
        "collection_hours": hours,
        "article_count": sum(len(v) for v in sections.values()),
        "stats": {
            "classified_total": len(classified),
            "dropped_unclassified": dropped_unclassified,
            "dropped_exact_duplicate": dropped_exact_dupe,
            "dropped_out_of_window": dropped_out_of_window,
            "before_algo_dedup": before_algo_dedup,
            "after_algo_dedup": after_algo_dedup,
            "after_llm_dedup": after_llm_dedup,
            "dropped_not_published_after_regrade": dropped_not_published,
            "dropped_invalid_section": dropped_invalid_section,
            "llm_dedup": llm_dedup_log,
            "published_grade_counts": grade_counts,
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
    print(f"  graded: {len(graded)}, exact-dedup: -{dropped_exact_dupe}, "
          f"window-filtered: -{dropped_out_of_window}, algo-dedup: {before_algo_dedup} -> {after_algo_dedup}, "
          f"llm-dedup: -> {after_llm_dedup}, regrade-dropped(C/X reps): -{dropped_not_published}")
    print(f"  grades: S={grade_counts.get('S', 0)} A={grade_counts.get('A', 0)} B={grade_counts.get('B', 0)}")


if __name__ == "__main__":
    main()
