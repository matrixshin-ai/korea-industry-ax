"""
Second-pass event dedup (requirement 5 of the "grade" rewrite).

dedup.py's algorithmic pass (title/number/policy matching) runs first and
catches most duplicates for free. This module runs a small, targeted Haiku
call ONLY on what's left: articles that still share a detected organization
name after the algorithmic pass - the same clue dedup.py itself uses, so
this only re-examines cases the cheap pass already flagged as suspicious but
couldn't confidently merge (e.g. different exact wording, differing reported
amounts). It never runs over the full published set, keeping cost small.

Only call this on the final PUBLISHED set (grade S/A/B) - never on rejected
(C/X) articles, and never before the algorithmic pass has already run.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dedup import _choose_better, _extract_organizations, _get_combined_text

MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 8000

SYSTEM_PROMPT = """당신은 한국어 뉴스 동일 사건 판별기입니다.
입력은 클러스터 배열이며, 각 클러스터는 {"c": 클러스터ID, "items": [{"i": id, "t": 제목,
"s": 매체명}, ...]} 형태입니다. 각 클러스터 "내부"의 기사들끼리만 비교하세요(클러스터를
넘나드는 비교는 하지 마세요). 진짜 같은 사건(같은 발표·같은 계약·같은 투자 건)을 다루는
기사들만 하나의 그룹으로 묶으세요. 발표·선정·착공·실증·성과처럼 사건의 단계가 다르면
절대 같은 그룹으로 묶지 마세요. 애매하면 묶지 말고 각자 별도 그룹으로 두세요.

출력은 JSON 배열만 반환하세요: [{"c": 클러스터ID, "groups": [[id, id], [id], ...]}, ...]
groups는 그 클러스터에 속한 모든 id를 정확히 한 번씩만 포함하는 부분집합들이어야 합니다.
설명, 코드블록, 다른 텍스트를 절대 추가하지 마세요.
"""


def build_clusters(items: list) -> list:
    """Group items that share a detected organization name (see dedup.py's
    ORGANIZATIONS list). Returns only groups with 2+ members - singletons
    have nothing to compare against and are skipped entirely."""
    org_map = {}
    for it in items:
        text = _get_combined_text(it)
        for org in _extract_organizations(text):
            org_map.setdefault(org, []).append(it)

    seen_keys = set()
    clusters = []
    for group in org_map.values():
        unique, seen_links = [], set()
        for it in group:
            link = it.get("link", "")
            if link in seen_links:
                continue
            seen_links.add(link)
            unique.append(it)
        if len(unique) < 2:
            continue
        key = tuple(sorted(seen_links))
        if key in seen_keys:
            continue
        seen_keys.add(key)
        clusters.append(unique)
    return clusters


def _parse_json_array(text: str):
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    return json.loads(text)


def llm_merge_clusters(client, clusters: list, log: dict) -> dict:
    """Ask Haiku to partition each cluster into same-event groups.
    Returns {absorbed_link: representative_link} for items that should merge;
    items not returned are left as their own representative."""
    if not clusters:
        return {}

    id_to_item = {}
    payload = []
    next_id = 0
    for ci, cluster in enumerate(clusters):
        cluster_items = []
        for it in cluster:
            iid = next_id
            next_id += 1
            id_to_item[iid] = it
            cluster_items.append({"i": iid, "t": it.get("title", ""), "s": it.get("source", "")})
        payload.append({"c": ci, "items": cluster_items})

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))}],
        )
        log["llm_dedup_input_tokens"] = response.usage.input_tokens
        log["llm_dedup_output_tokens"] = response.usage.output_tokens
        text = "".join(b.text for b in response.content if b.type == "text")
        parsed = _parse_json_array(text)
    except Exception as e:  # noqa: BLE001 - this pass failing must not break the run
        log["llm_dedup_error"] = str(e)
        return {}

    merge_map = {}
    for entry in parsed:
        for group in entry.get("groups", []):
            group_items = [id_to_item[i] for i in group if i in id_to_item]
            if len(group_items) < 2:
                continue
            rep = group_items[0]
            for other in group_items[1:]:
                rep = _choose_better(rep, other)
            for it in group_items:
                if it is not rep:
                    merge_map[it.get("link", "")] = rep.get("link", "")
    return merge_map


def apply_merge_map(items: list, merge_map: dict) -> list:
    """Fold absorbed items into their representative's `related` list and
    drop them from the top-level result."""
    by_link = {it.get("link", ""): it for it in items}
    absorbed_into = {}
    for absorbed_link, rep_link in merge_map.items():
        # Follow chains (A absorbed into B, B absorbed into C) to the final root.
        seen = {absorbed_link}
        root = rep_link
        while root in merge_map and root not in seen:
            seen.add(root)
            root = merge_map[root]
        absorbed_into.setdefault(root, []).append(absorbed_link)

    result = []
    for it in items:
        link = it.get("link", "")
        if link in merge_map:
            continue  # folded into another representative below
        merged = dict(it)
        extra_related = [
            {"source": by_link[l].get("source", ""), "link": l}
            for l in absorbed_into.get(link, [])
            if l in by_link
        ]
        if extra_related:
            merged["related"] = list(merged.get("related", [])) + extra_related
        result.append(merged)
    return result
