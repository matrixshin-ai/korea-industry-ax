"""
classified.json -> public/data.json

1. Use only this run's fresh candidates (no carry-over from a previous day),
   keeping every graded item (S/A/B/C/X) at this stage - not just S/A - so
   a same-event duplicate that individually landed on B, C or X can still
   ride along as a `related` citation under a higher-graded representative.
2. Drop exact-duplicate URLs (after normalization).
3. Apply the KST collection window (same helper collect.py used).
4. Merge same-event duplicates: dedup.py's algorithmic pass first (exact
   title / URL / org+number-or-quote-or-policy gated similarity), then one
   Haiku pass (jobs/llm_dedup.py) over the FULL TITLES of every group that
   could still end up published (S or A somewhere in the group) - not just
   groups sharing a detected organization name - split into chunks of at
   most llm_dedup.MAX_CHUNK_SIZE items per call so one day's publishable
   volume never exceeds a single call's context. Different stages of the
   same story (발표/선정/착공/실증/성과) are never merged.
5. Event-level grade unification: for each merged group, the group's grade/
   section/etc become whichever single member ranks highest by (grade,
   score); if that member is S/A but its title doesn't itself name AI/AX
   (e.g. it was picked for an unrelated angle inside an AX-event group),
   fall back to the highest-ranked S/A member whose title does name AI/AX.
   Every other member - regardless of its own grade - becomes a `related`
   citation under that representative.
6. Keep only groups whose representative is S or A (B is a demotion target
   only - see classify.py - and is never published, regardless of
   ulsan_score; an unclassified item just retries whenever it's re-collected).
7. score = grade_points(S=100/A=70) + ulsan_score + core*10. Apply the
   daily cap (DAILY_CAP=200) across all sections by score desc, then newest
   first; then sort each section the same way.
8. Write public/data.json with generation stats for the footer/status line.
9. Write logs/yield_YYYYMMDD_HHMM.json: per-RSS-feed and per-Naver-query
   candidate counts and S/A counts (each candidate's own grade, before any
   dedup merging) - for a manual look after a week of runs at which sources
   are actually finding S/A-worthy content. Nothing is pruned automatically.
"""
import glob
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from classify import GRADE_POINTS, GRADES, has_ai_term
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
DAILY_CAP = 200
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


def compute_yield_log(classified):
    """Per-RSS-feed and per-Naver-query candidate/S/A counts, using each
    candidate's own grade (before any dedup merging) - a feed or query that
    gets merged away a lot is a redundancy signal, not a yield signal, so
    dedup'd-away duplicates still count here."""
    rss = {}
    naver = {}
    for c in classified:
        grade = c.get("grade")
        feed = c.get("origin_feed")
        query = c.get("origin_query")
        if feed:
            entry = rss.setdefault(feed, {"source": c.get("source", ""), "candidates": 0, "s": 0, "a": 0})
            entry["candidates"] += 1
            if grade in ("S", "A"):
                entry[grade.lower()] += 1
        if query:
            entry = naver.setdefault(query, {"candidates": 0, "s": 0, "a": 0})
            entry["candidates"] += 1
            if grade in ("S", "A"):
                entry[grade.lower()] += 1
    return {"rss": rss, "naver": naver}


def _all_members(item):
    """Flatten item + any nested `related` members (from either dedup pass)
    into one flat list - a merged group can be nested one or two levels deep
    depending on which pass(es) touched it."""
    members = [item]
    for rel in item.get("related", []) or []:
        members.extend(_all_members(rel))
    return members


def _group_has_publishable_grade(item):
    return any(is_publishable(m) for m in _all_members(item))


def is_publishable(item) -> bool:
    # Requirement 1: B is a demotion target only (classify.py) and is never
    # published, regardless of ulsan_score.
    return item.get("grade") in ("S", "A")


def item_score(item) -> int:
    return GRADE_POINTS.get(item.get("grade"), 0) + (item.get("ulsan_score") or 0) + (item.get("core") or 0) * 10


def _sort_key(item):
    pub_dt = parse_published(item.get("published"))
    return (item_score(item), pub_dt.astimezone(timezone.utc).isoformat() if pub_dt else "")


def _member_rank(m):
    """(grade rank, score) - the sort key used to pick a merged group's
    representative (requirement 4: highest grade, then highest score)."""
    return (GRADE_ORDER.get(m.get("grade"), 0), item_score(m))


def regrade_representatives(items):
    """Unify each merged group onto its single highest-graded, highest-scored
    member's grade/section/etc (requirement 4). If that member is S/A but its
    title doesn't itself name AI/AX - e.g. picked for an unrelated angle, like
    "군함 건조" inside a summit-AX event group - fall back to the
    highest-ranked S/A member whose title does name AI/AX, so the published
    headline always reflects the event's AX content. Every other member -
    whatever its own grade - becomes a `related` citation under that
    representative."""
    result = []
    for item in items:
        members = _all_members(item)
        best = max(members, key=_member_rank)
        if best.get("grade") in ("S", "A") and not has_ai_term(best.get("title", "")):
            ax_titled = [m for m in members
                         if m.get("grade") in ("S", "A") and has_ai_term(m.get("title", ""))]
            if ax_titled:
                best = max(ax_titled, key=_member_rank)
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
    # Cost cut: a group made entirely of B/C/X members will never get
    # published regardless of how it's merged, so there's no point spending a
    # Haiku call on grouping it - only groups that could still end up
    # published (S or A somewhere in the group) go to the full-title pass.
    merge_candidates = [it for it in deduped if _group_has_publishable_grade(it)]
    llm_dedup_log["candidate_groups"] = len(merge_candidates)
    llm_dedup_log["candidate_groups_skipped_no_publishable_grade"] = len(deduped) - len(merge_candidates)
    if merge_candidates and api_key:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        merge_map = llm_dedup.llm_merge_candidates(client, merge_candidates, llm_dedup_log)
        deduped = llm_dedup.apply_merge_map(deduped, merge_map)
    elif merge_candidates:
        print(f"  ANTHROPIC_API_KEY not set - skipping LLM dedup pass on {len(merge_candidates)} candidate groups")
    after_llm_dedup = len(deduped)

    regraded = regrade_representatives(deduped)
    published = [item for item in regraded if is_publishable(item)]
    dropped_not_published = len(regraded) - len(published)

    valid = []
    dropped_invalid_section = 0
    for item in published:
        if item.get("section") not in (1, 2, 3):
            # Defensive: classify.py validates `section` before ever writing a
            # non-X grade, so this shouldn't happen - but a malformed Haiku
            # response is cheaper to skip here than to crash the whole build on.
            dropped_invalid_section += 1
            continue
        item["score"] = item_score(item)
        valid.append(item)

    valid.sort(key=_sort_key, reverse=True)
    pre_cap_section_counts = {k: sum(1 for it in valid if str(it["section"]) == k) for k in ("1", "2", "3")}
    capped = valid[:DAILY_CAP]
    dropped_over_cap = len(valid) - len(capped)

    sections = {"1": [], "2": [], "3": []}
    for item in capped:  # already in final order - per-section lists stay sorted
        sections[str(item["section"])].append(item)

    for key in sections:
        for it in sections[key]:
            it.pop("id", None)
            it.pop("collected_via", None)
            it.pop("unclassified", None)
            it.pop("prefiltered", None)
            it.pop("origin_feed", None)
            it.pop("origin_query", None)
            it.pop("rule_capped_from", None)
            it.pop("finance_filtered", None)

    run_log = load_latest_run_log() or {}

    grade_counts = {"S": 0, "A": 0}
    for item in capped:
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
            "publishable_before_cap": len(valid),
            "publishable_before_cap_by_section": pre_cap_section_counts,
            "daily_cap": DAILY_CAP,
            "dropped_over_cap": dropped_over_cap,
            "finance_title_filtered": sum(1 for c in classified if c.get("finance_filtered")),
            "rule_capped": sum(1 for c in classified if c.get("rule_capped_from")),
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

    os.makedirs(LOGS_DIR, exist_ok=True)
    yield_log = compute_yield_log(classified)
    yield_path = os.path.join(LOGS_DIR, f"yield_{datetime.now(KST).strftime('%Y%m%d_%H%M')}.json")
    with open(yield_path, "w", encoding="utf-8") as f:
        json.dump(yield_log, f, ensure_ascii=False, indent=2)

    print(f"Built public/data.json: {data['article_count']} articles "
          f"(section1={len(sections['1'])}, section2={len(sections['2'])}, section3={len(sections['3'])})")
    print(f"  graded: {len(graded)}, exact-dedup: -{dropped_exact_dupe}, "
          f"window-filtered: -{dropped_out_of_window}, algo-dedup: {before_algo_dedup} -> {after_algo_dedup}, "
          f"llm-dedup: -> {after_llm_dedup}, not-published: -{dropped_not_published}")
    print(f"  cap: {len(valid)} publishable {pre_cap_section_counts} -> {len(capped)} (cap {DAILY_CAP})")
    print(f"  grades: S={grade_counts.get('S', 0)} A={grade_counts.get('A', 0)}")
    print(f"  yield log: {yield_path} ({len(yield_log['rss'])} feeds, {len(yield_log['naver'])} queries)")


if __name__ == "__main__":
    main()
